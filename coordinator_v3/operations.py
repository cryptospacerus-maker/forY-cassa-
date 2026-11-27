"""Operator preflight and explicit mutable Git store configuration."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from coordinator.context import Blocked, git, SHA
from coordinator.engine import base_files, make_commit
from coordinator_v2.runtime import DockerSandbox, IMAGE, Project

from .queue import transaction

SUPPORTED_CLI = "codex-cli 0.172.0"


def configure_git(root):
    store = root / "git"
    if store.is_symlink() or not store.is_dir() or store.stat().st_mode & 0o077:
        raise Blocked("Подготовьте закрытый bare Git store в каталоге состояния: git.")
    os.environ["COORDINATOR_V3_GIT_STORE"] = str(store)
    if git("rev-parse", "--is-bare-repository").strip() != b"true":
        raise Blocked("Git store должен быть bare.")


def verify_origin(repo):
    url = f"https://github.com/{repo}"
    remotes = [git("remote", "get-url", *flags, "--all", "origin").decode().splitlines()
               for flags in ((), ("--push",))]
    if any(len(lines) != 1 or lines[0].lower() not in {url.lower(), url.lower() + ".git"} for lines in remotes):
        raise Blocked("Git origin для чтения и записи должен указывать на ожидаемый HTTPS-репозиторий.")


def ensure_base(repo, base):
    verify_origin(repo)
    try:
        git("cat-file", "-e", f"{base}^{{commit}}")
    except Blocked:
        git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        git("cat-file", "-e", f"{base}^{{commit}}")


def preflight(queue, root, repo, base, image=None):
    verify_origin(repo)
    if not SHA.fullmatch(base):
        raise Blocked("Нужен точный базовый SHA.")
    git("cat-file", "-e", f"{base}^{{commit}}")
    run = uuid4().hex
    ref = f"refs/coordinator-v3/preflight/{run}"
    path = "preflight/probe.txt"
    with TemporaryDirectory(dir=root, prefix="preflight-") as temp:
        candidate = make_commit(base, {path: run}, run, 1)
        git("update-ref", ref, candidate)
        try:
            if git("rev-parse", ref).decode().strip() != candidate or base_files(candidate, [path])[path] != run:
                raise Blocked("Запись Git objects/refs не прошла проверку.")
            probe = Path(temp) / "probe"
            probe.write_text(run)
            if probe.read_text() != run:
                raise Blocked("Временный каталог недоступен.")
            with transaction(queue.path) as db:
                db.execute("SAVEPOINT preflight")
                db.execute("CREATE TABLE preflight_probe (value TEXT)")
                db.execute("INSERT INTO preflight_probe VALUES (?)", (run,))
                if db.execute("SELECT value FROM preflight_probe").fetchone()[0] != run:
                    raise Blocked("SQLite preflight не смог прочитать записанное значение.")
                db.execute("ROLLBACK TO preflight")
                db.execute("RELEASE preflight")
            if image:
                if not IMAGE.fullmatch(image):
                    raise Blocked("Нужен точный ID образа QA.")
                docker = DockerSandbox(image, Project.load(base, "lab"), base)
                checks = docker.project.for_role("tester", {"tests", "browser"})
                # main intentionally contains the defective fixture. Require real container exit both times.
                for check in checks:
                    evidence = docker.run(check, base)
                    if evidence.status not in {"pass", "fail"} or evidence.exit_code is None:
                        raise Blocked(f"Контейнерная проверка {check.name} не исполнилась.")
        finally:
            git("update-ref", "-d", ref, candidate)
    return {"git": "ok", "sqlite": "ok", "temp": "ok", "container_probe": bool(image)}


def cli_preflight(command):
    binary = shutil.which(command)
    if not binary:
        raise Blocked("Codex CLI недоступен.")
    env = {"PATH": os.environ["PATH"], "LANG": "C"}
    with TemporaryDirectory(prefix="v3-cli-check-") as home:
        env.update(HOME=home, CODEX_HOME=home)
        def inspect(*args):
            return subprocess.run([binary, *args], capture_output=True, text=True,
                                  timeout=5, check=True, env=env).stdout

        if inspect("--version").strip() != SUPPORTED_CLI:
            raise Blocked(f"Для пилота требуется {SUPPORTED_CLI}.")
        for args in (("exec",), ("exec", "resume")):
            help_text = inspect(*args, "--help")
            required = {"--json", "--output-schema", "--output-last-message", "--ignore-user-config"}
            if args == ("exec",):
                required |= {"--sandbox", "--ignore-rules", "--disable"}
            if not all(flag in help_text for flag in required):
                raise Blocked("CLI не поддерживает закреплённый интерфейс V3.")
    return binary
