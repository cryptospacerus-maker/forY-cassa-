import argparse
import datetime
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .common import ROOT, dump, git, read_json
from .collect import collect, digest
from .model import assess
from .validate import validate_report
from .render import render

def safe_error(exc):
    from urllib.error import HTTPError, URLError
    from jsonschema.exceptions import ValidationError
    if isinstance(exc, HTTPError):
        return f'API HTTP {exc.code}; проверьте доступ/лимиты. Автоповтор отключён.'
    if isinstance(exc, URLError):
        return 'Сетевая ошибка API. Статус оплаты неизвестен; автоповтор отключён.'
    if isinstance(exc, ValidationError):
        return 'Нарушена JSON-схема: ' + '.'.join(str(p) for p in exc.absolute_path)
    if type(exc) is ValueError:
        # Only fixed application messages, never arbitrary provider content.
        return str(exc)[:200]
    return type(exc).__name__


def run(request, output, context, dry_run=False, response_file=None):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    rubric = read_json(ROOT/'evaluation/rubric.json')
    schema = read_json(ROOT/'evaluation/schemas/report.json')
    profile = (ROOT/'agents/evaluator.md').read_text() + '\n' + (ROOT/'evaluation/jury-research.md').read_text()
    meta = {**request, 'control_sha':git('rev-parse','HEAD').decode().strip(),
            'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'rubric_version':rubric['version'],'rubric_sha256':digest(json.dumps(rubric,sort_keys=True).encode()),'profile_sha256':digest(profile.encode())}
    try:
        Draft202012Validator(read_json(ROOT/'evaluation/schemas/input.json')).validate(request)
        packet = collect(request, context)
        dump(out/'manifest.json', {**packet, 'evidence':[{k:v for k,v in e.items() if k!='content'} for e in packet['evidence']]})
        meta['manifest_sha256'] = packet['manifest_sha256']
        if dry_run:
            report = {'summary':'Технический прогон сбора доказательств. Конкурсные баллы не выставлялись.',
                      'status':'dry_run','limitations':['LLM не вызывалась; это не оценка решения.']}
        else:
            if response_file:
                assessment = read_json(response_file)
                meta['model'] = 'fixture-only'
            else:
                if not context.get('github_issue') or not context.get('main_instructions'):
                    raise ValueError('Live evaluation requires current GitHub issue and main instructions')
                assessment, usage = assess(profile, schema, rubric, packet)
                meta.update(usage)
            totals = validate_report(assessment, schema, rubric, packet['evidence'])
            report = {**assessment, 'totals':totals,
                      'status':('fixture' if response_file else ('reviewed' if totals['unassessed_max']==0 else 'partial'))}
        report['metadata'] = meta
    except Exception as exc:
        # Do not put raw HTTP bodies, keys, model output or arbitrary exception data in public logs.
        report = {'metadata':meta,'status':'blocked','summary':'Оценка не завершена.',
                  'blockers':[safe_error(exc)],
                  'limitations':['Проверьте вход, доступность файлов, схему ответа и настройки бюджета/API. Баллы не опубликованы.']}
    dump(out/'report.json', report)
    (out/'report.md').write_text(render(report,rubric))
    return report

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--request',required=True); p.add_argument('--output',required=True)
    p.add_argument('--context'); p.add_argument('--dry-run',action='store_true')
    p.add_argument('--response-file',help='Offline fixture only; never a real model verdict')
    a=p.parse_args()
    result=run(read_json(a.request),a.output,read_json(a.context) if a.context else {},a.dry_run,a.response_file)
    print(result['status'])
    raise SystemExit(1 if result['status']=='blocked' else 0)

if __name__=='__main__': main()
