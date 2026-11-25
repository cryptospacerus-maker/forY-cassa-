"""Convenient local entry point with authenticated, current project context."""
import argparse
import base64
import datetime
from pathlib import Path
from .common import REPO, dump
from .github_io import api, comments
from .run import run


def main():
    p=argparse.ArgumentParser(description='Судья: локальная оценка, без публикации в GitHub')
    p.add_argument('--issue',type=int,required=True)
    p.add_argument('--sha',required=True)
    p.add_argument('--track',choices=['business','integrator'],required=True)
    p.add_argument('--case-version',required=True)
    p.add_argument('--manifest',default='evaluation/evidence/manifest.json')
    p.add_argument('--request-id',required=True)
    p.add_argument('--output',default='.judge-output')
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    request={'issue_number':a.issue,'target_sha':a.sha,'track':a.track,'case_version':a.case_version,
             'evidence_manifest_path':a.manifest,'request_id':a.request_id}
    issue=api(f'repos/{REPO}/issues/{a.issue}')
    context={'github_issue':{'title':issue['title'],'body':issue['body'],
                            'comments':[{'body':c['body'],'url':c['html_url']} for c in comments(a.issue)]},
             'main_instructions':{},'retrieved_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    for path in ('AGENTS.md','PROJECT.md'):
        data=api(f'repos/{REPO}/contents/{path}?ref=main')
        context['main_instructions'][path]=base64.b64decode(data['content']).decode()
    if Path(a.output,'report.json').exists():
        raise ValueError('Use a fresh output directory to preserve earlier report')
    result=run(request,a.output,context,a.dry_run)
    print(result['status'],str(Path(a.output,'report.md').resolve()))
    raise SystemExit(1 if result['status']=='blocked' else 0)

if __name__=='__main__': main()
