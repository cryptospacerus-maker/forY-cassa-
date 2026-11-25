"""Collect evidence and validate an assessment authored in the Codex session."""
import argparse
import datetime
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from .collect import collect, digest
from .common import ROOT, dump, git, read_json
from .render import render
from .validate import validate_report

EVALUATION = ROOT / 'evaluation'


def prepare(request, context, output):
    Draft202012Validator(read_json(EVALUATION / 'schemas/input.json')).validate(request)
    if not context.get('github_issue') or not context.get('main_instructions'):
        raise ValueError('Current GitHub issue and main instructions are required')
    packet = collect(request, context)
    packet['control_sha'] = git('rev-parse', 'HEAD').decode().strip()
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'packet.json', packet)
    return digest((output / 'packet.json').read_bytes())


def finalize(output, assessment_path):
    packet_path = output / 'packet.json'
    packet = read_json(packet_path)
    submission = read_json(assessment_path)
    packet_digest = digest(packet_path.read_bytes())
    if submission.get('packet_sha256') != packet_digest:
        raise ValueError('Assessment must reference the SHA-256 of the prepared packet')
    if packet['control_sha'] != git('rev-parse', 'HEAD').decode().strip():
        raise ValueError('Use the same evaluator checkout as for packet preparation')
    if (output / 'report.json').exists():
        raise ValueError('This packet already has a finalized report')

    rubric_path = EVALUATION / 'rubric.json'
    rubric = read_json(rubric_path)
    assessment = submission['assessment']
    totals = validate_report(assessment, read_json(EVALUATION / 'schemas/report.json'),
                             rubric, packet['evidence'])
    report = {
        **assessment, 'totals': totals,
        'status': 'partial' if totals['unassessed_max'] else 'reviewed',
        'metadata': {
            **packet['request'], 'control_sha': packet['control_sha'],
            'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'rubric_version': rubric['version'], 'rubric_sha256': digest(rubric_path.read_bytes()),
            'manifest_sha256': packet['manifest_sha256'], 'packet_sha256': packet_digest,
            'evaluation_mode': 'codex-session',
        },
    }
    dump(output / 'manifest.json', {
        **{key: packet[key] for key in ('request', 'control_sha', 'manifest_sha256')},
        'evidence': [{key: value for key, value in item.items() if key != 'content'}
                     for item in packet['evidence']],
    })
    (output / 'report.md').write_text(render(report, rubric))
    dump(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_cli = commands.add_parser('prepare')
    prepare_cli.add_argument('--request', required=True)
    prepare_cli.add_argument('--context', required=True)
    finish_cli = commands.add_parser('finalize')
    finish_cli.add_argument('--assessment', required=True, type=Path)
    for command in (prepare_cli, finish_cli):
        command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(prepare(read_json(args.request), read_json(args.context), args.output))
        else:
            print(finalize(args.output, args.assessment)['status'])
    except ValidationError as exc:
        print('Invalid JSON schema at ' + '.'.join(map(str, exc.absolute_path)), file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError) as exc:
        print(f'Judge blocked: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
