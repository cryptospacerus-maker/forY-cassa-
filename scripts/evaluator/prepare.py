import json
import os
from pathlib import Path
from jsonschema import Draft202012Validator
from .common import ROOT, REPO, dump, read_json, git
from .github_io import api, comments


def prepare():
    event=read_json(os.environ['GITHUB_EVENT_PATH'])
    if event.get('repository',{}).get('full_name') != REPO:
        raise ValueError('Unexpected repository')
    if os.environ['GITHUB_REF'] != 'refs/heads/main':
        raise ValueError('Only trusted main workflow may run')
    actor=os.environ['GITHUB_ACTOR']
    allowed=os.environ.get('JUDGE_ALLOWED_ACTORS','').split(',')
    if actor not in [x.strip() for x in allowed if x.strip()]:
        raise ValueError('Configure JUDGE_ALLOWED_ACTORS with approved GitHub logins')
    permission=api(f'repos/{REPO}/collaborators/{actor}/permission')['permission']
    if permission not in ('admin','maintain','write'):
        raise ValueError('Actor lacks repository write permission')
    inputs=event['inputs']
    request={k:inputs[k] for k in ('target_sha','track','case_version','evidence_manifest_path','request_id')}
    request['issue_number']=int(inputs['issue_number'])
    Draft202012Validator(read_json(ROOT/'evaluation/schemas/input.json')).validate(request)
    issue=request['issue_number']
    snapshot=api(f'repos/{REPO}/issues/{issue}')
    history=comments(issue)
    marker=f"<!-- judge-request:{request['request_id']} -->"
    prior=[c for c in history if marker in c['body'] and c['user']['login']=='github-actions[bot]']
    if prior:
        raise ValueError('Request already reserved; use existing report or a new request_id after diagnosis')
    context={'github_issue':{'title':snapshot['title'],'body':snapshot['body'],'url':snapshot['html_url'],
                             'comments':[{'body':c['body'],'url':c['html_url']} for c in history]},
             'main_instructions':{p:git('show',f'HEAD:{p}').decode() for p in ('AGENTS.md','PROJECT.md')},
             'initiator':actor}
    if len(json.dumps(context).encode())>100_000:
        raise ValueError('Issue context too large; create a focused evaluation issue with source links')
    reservation=api(f'repos/{REPO}/issues/{issue}/comments','POST',
       {'body':marker+f'\nСудья: запуск зарезервирован для @{actor}. Версия `{request["target_sha"]}`.\n'
        +f'https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}\n'
        +'Повтор того же request_id не вызывает модель повторно.'})
    Path('judge-input').mkdir(exist_ok=True)
    dump('judge-input/request.json',request)
    dump('judge-input/context.json',context)
    dump('judge-input/delivery.json',{'comment_id':reservation['id'],'actor':actor,'marker':marker,'issue':issue})

if __name__=='__main__': prepare()
