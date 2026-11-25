"""Deliver a failure notice from a trusted, completed coordinator dispatch."""

import json
import os
from pathlib import Path
import re
import sys

from .__main__ import add_comment, make_meta, marker, previous_report, reservations, run_url
from .context import Blocked, api

TITLE = re.compile(r"Coordinator issue=([1-9][0-9]{0,8}) mode=(dry-run|demo|task) request=([A-Za-z0-9][A-Za-z0-9_-]{0,39})")
FAILURES = {"failure", "timed_out", "cancelled", "startup_failure", "action_required", "stale"}


def notify(repo, event):
    run = event["workflow_run"]
    if (
        event["action"] != "completed"
        or run["event"] != "workflow_dispatch"
        or run["path"] != ".github/workflows/coordinator.yml"
        or run["head_branch"] != "main"
        or run["head_repository"]["full_name"].lower() != repo.lower()
        or run["conclusion"] not in FAILURES
    ):
        return
    title = TITLE.fullmatch(run["display_title"])
    if not title:
        return
    issue, mode, request = title.groups()
    meta = make_meta(repo, int(issue), run["actor"]["login"], request, mode,
                     run["head_sha"], str(run["id"]))
    items = reservations(meta)
    markers = {(item.get("body") or "").split("\n", 1)[0] for item in items}
    if (
        marker(meta, "claim") not in markers
        or marker(meta, "interrupted") in markers
        or previous_report(items, meta)
    ):
        return
    latest = api(repo, f"actions/runs/{meta['workflow']}")
    if latest["run_attempt"] != run["run_attempt"] or latest["status"] != "completed":
        return
    add_comment(
        meta, f"{marker(meta, 'interrupted')}\n@{meta['actor']}\n\n"
        "Запуск завершился аварийно или был отменён до итогового отчёта. "
        f"[Откройте журнал запуска]({run_url(meta)}).\n\n"
        "Для восстановления выберите **Re-run all jobs**. Для платного запроса с тем же "
        "ключом восстановление использует только завершённое состояние. Если оно недоступно, "
        "сверьте расходы и отдельно согласуйте запуск с новым ключом.",
    )


if __name__ == "__main__":
    try:
        notify(os.environ["GITHUB_REPOSITORY"], json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
    except (Blocked, KeyError, OSError, ValueError):
        print("Не удалось доставить аварийное уведомление координатора.", file=sys.stderr)
        sys.exit(1)
