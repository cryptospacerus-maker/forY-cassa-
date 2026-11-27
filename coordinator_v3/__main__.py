"""Local operator commands and explicit owner-controlled worker startup."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from threading import Event, Thread
import subprocess

from coordinator.context import Blocked, identity, REPO
from coordinator_v2.runtime import CHECK_ID, FEATURES, IMAGE

from . import github
from .cli import Adapter
from .api import GitHubAPI
from .operations import SUPPORTED_CLI, cli_preflight, configure_git, preflight
from .engine import Deferred, advance
from .queue import Conflict, Queue, transaction
from .spike import ROUTES
from .transport import Client, server
from .worker import Worker

DEFAULT_REPO = "cryptospacerus-maker/forY-cassa-"


@contextmanager
def operator_lock(root):
    with (root / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def project_args(parser):
    parser.add_argument("--project", default="lab")
    parser.add_argument("--features", nargs="+", choices=sorted(FEATURES), default=["tests", "browser"])
    parser.add_argument("--image", required=True, help="Immutable local Docker image ID")


def parser():
    root = argparse.ArgumentParser(prog="python3 -m coordinator_v3")
    root.add_argument("--state", type=Path, required=True)
    command = root.add_subparsers(dest="command", required=True)
    serve = command.add_parser("serve")
    serve.add_argument("--credentials", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8763)
    serve.add_argument("--advance-github", action="store_true")
    serve.add_argument("--reader-token", type=Path)
    worker = command.add_parser("worker")
    worker.add_argument("--id", choices=sorted(set(ROUTES.values())), required=True)
    worker.add_argument("--account", required=True, help="Opaque alias, agreed with the coordinator")
    worker.add_argument("--url", required=True)
    worker.add_argument("--token-file", type=Path, required=True)
    worker.add_argument("--codex", default="codex")
    worker.add_argument("--enable-live-cli", action="store_true", required=True)
    command.add_parser("status")
    command.add_parser("report").add_argument("run")
    command.add_parser("resume-worker").add_argument("worker", choices=sorted(set(ROUTES.values())))
    command.add_parser("cancel").add_argument("run")
    review = command.add_parser("review-job")
    review.add_argument("job")
    review.add_argument("resolution", choices=["confirmed_stopped", "confirmed_finished", "outcome_unknown"])
    probe = command.add_parser("preflight")
    probe.add_argument("--repo", default=DEFAULT_REPO)
    probe.add_argument("--base", required=True)
    probe.add_argument("--image")
    tick = command.add_parser("advance")
    tick.add_argument("--github", action="store_true")
    tick.add_argument("--reader-token", type=Path)
    ingest = command.add_parser("ingest")
    ingest.add_argument("--repo", default=DEFAULT_REPO)
    ingest.add_argument("--issue", type=int, required=True)
    ingest.add_argument("--comment", type=int, required=True)
    ingest.add_argument("--reader-token", type=Path, required=True)
    project_args(ingest)
    publish = command.add_parser("publish")
    publish.add_argument("--run", required=True)
    publish.add_argument("--publisher", required=True)
    publish.add_argument("--publisher-token", type=Path, required=True)
    return root


def tick(queue, root, cache):
    def current(spec):
        if spec["mode"] == "fixture":
            return spec["snapshot"]
        if cache is None:
            raise Deferred
        return cache(spec)

    advance(queue, root, current)


def reader(path):
    if path is None:
        raise Blocked("Для GitHub-проверки требуется --reader-token.")
    return github.SnapshotCache(GitHubAPI(path))


def execute(args, root):
    if args.command == "worker":
        binary = cli_preflight(args.codex)
        client = Client(args.url, args.token_file.read_text().strip())
        Worker(client, root, args.id, args.account, SUPPORTED_CLI, Adapter(binary), mode="github").loop()
        return
    queue = Queue(root / "queue.sqlite")
    if args.command in {"serve", "advance", "ingest", "publish", "preflight"}:
        configure_git(root)
    if args.command == "serve":
        cache = reader(args.reader_token) if args.advance_github else None
        with operator_lock(root):
            queue.revoke()
        with server(args.port, queue, json.loads(args.credentials.read_text()),
                    allowed_version=SUPPORTED_CLI) as http:
            stopped = Event()

            def schedule():
                while not stopped.is_set():
                    try:
                        with operator_lock(root):
                            tick(queue, root, cache)
                    except BlockingIOError:
                        pass
                    except Exception as error:
                        # Keep the authenticated worker endpoint available during a failed tick.
                        queue.revoke()
                        print(f"Coordinator tick failed: {type(error).__name__}", flush=True)
                    stopped.wait(5)

            scheduler = Thread(target=schedule, daemon=True)
            scheduler.start()
            try:
                http.serve_forever()
            finally:
                stopped.set()
                scheduler.join()
        return
    if args.command == "status":
        return queue.status()
    if args.command == "report":
        with transaction(queue.path) as db:
            row = db.execute("SELECT result FROM runs WHERE id=?", (args.run,)).fetchone()
        if not row or row["result"] is None:
            raise Blocked("Итоговый отчёт пока недоступен.")
        return json.loads(row["result"])
    if args.command == "cancel":
        queue.finish(args.run, {"status": "cancelled", "reason": "Остановлено оператором.", "checks": []})
        return
    with operator_lock(root):
        if args.command == "advance":
            tick(queue, root, reader(args.reader_token) if args.github else None)
        elif args.command == "resume-worker":
            queue.unpause(args.worker)
        elif args.command == "review-job":
            queue.review(args.job, args.resolution)
        elif args.command == "preflight":
            return preflight(queue, root, identity(args.repo, REPO), args.base, args.image)
        elif args.command == "publish":
            transport = GitHubAPI(args.publisher_token)
            transport.publisher(args.publisher)
            with transport.installed():
                return github.publish(queue, args.run, args.publisher)
        elif args.command == "ingest":
            identity(args.image, IMAGE)
            identity(args.project, CHECK_ID)
            features = sorted(set(args.features))
            if not {"tests", "browser"}.intersection(features):
                raise Blocked("Нужна хотя бы одна проверка QA.")
            identity(args.repo, REPO)
            with GitHubAPI(args.reader_token).installed():
                return github.ingest(queue, args.repo, args.issue, args.comment, args.project, features, args.image)


def main():
    os.umask(0o077)
    options = parser().parse_args()
    try:
        root = options.state.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise Blocked("Каталог состояния должен быть доступен только владельцу (0700).")
        result = execute(options, root)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
    except (Blocked, Conflict, Deferred, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error) or "Операция недоступна. Проверьте состояние сервиса.") from None


if __name__ == "__main__":
    main()
