import os
from pathlib import Path
from .common import REPO, read_json
from .github_io import api


def publish():
    delivery=read_json('judge-input/delivery.json')
    report=Path('judge-output/report.md')
    content=report.read_text() if report.exists() else 'Оценка заблокирована: этап выполнения завершился без отчёта. Проверьте журнал запуска.'
    # Comment limit; complete report remains a workflow artifact.
    content=content[:45000]
    body=(delivery['marker']+f'\n@{delivery["actor"]}\n'+content+
          f'\nПолные файлы отчёта и манифест: https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}\n')
    api(f'repos/{REPO}/issues/comments/{delivery["comment_id"]}','PATCH',{'body':body})

if __name__=='__main__': publish()
