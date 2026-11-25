"""Trusted project settings, immutable Git snapshots and isolated checks."""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from coordinator.context import Blocked, git, identity, SHA

FEATURES = {"execution", "tests", "browser", "mcp"}
ROLES = {"developer", "interface"}
SAFE_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
CHECK_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
MAX_EXPORT = 2 * 1024 * 1024
MAX_LOG = 64 * 1024
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy")


@dataclass(frozen=True)
class Check:
    name: str
    feature: str
    roles: tuple
    argv: tuple
    timeout: int
    owners: tuple = ()


@dataclass(frozen=True)
class Project:
    source: str
    harness: str
    checks: dict

    @classmethod
    def load(cls, base, name):
        identity(name, CHECK_ID)
        data = json.loads(git("show", f"{identity(base, SHA)}:coordinator_v2/projects/{name}.json"))
        source, harness = data["source"], data["harness"]
        protected = {"agents", "coordinator", "dashboard", "evaluation", "pilot"}
        if (
            not all(SAFE_PATH.fullmatch(path) for path in (source, harness))
            or len(source.split("/")) < 2 or len(harness.split("/")) < 2
            or source.split("/")[0] in protected or harness.split("/")[0] in protected
            or (source.startswith("coordinator_v2/") and source != "coordinator_v2/demo/app")
            or source == harness or source.startswith(harness + "/") or harness.startswith(source + "/")
        ):
            raise Blocked("Небезопасная область приложения или проверок.")
        checks = {}
        for check, item in data["checks"].items():
            identity(check, CHECK_ID)
            feature, roles, argv, timeout = (item[key] for key in ("feature", "roles", "argv", "timeout"))
            expected = ROLES if feature == "execution" else {"tester"}
            owners = item.get("repair", [])
            if (
                feature not in {"execution", "tests", "browser"} or not roles or not set(roles) <= expected
                or not 1 <= len(argv) <= 12 or argv[0] not in {"python3", "node"}
                or not all(isinstance(arg, str) and 0 < len(arg) <= 300 and "\0" not in arg for arg in argv)
                or not set(owners) <= ROLES
                or type(timeout) is not int or not 1 <= timeout <= 75
            ):
                raise Blocked("Профиль проверки некорректен.")
            checks[check] = Check(check, feature, tuple(roles), tuple(argv), timeout, tuple(owners))
        if len(checks) > 12:
            raise Blocked("Профиль содержит слишком много проверок.")
        return cls(source, harness, checks)

    def for_role(self, role, features):
        return [check for check in self.checks.values() if role in check.roles and check.feature in features]

    def allows(self, path):
        return (
            isinstance(path, str) and len(path) <= 180 and SAFE_PATH.fullmatch(path)
            and path.startswith(self.source + "/")
            and not path.startswith(("coordinator_v2/projects/", "coordinator_v2/sandbox/"))
            and Path(path).suffix in {".py", ".js", ".css", ".html", ".json", ".md", ".txt"}
        )


def materialize(sha, prefix, directory):
    """Export ordinary blobs only, without checkout, hooks, filters or symlinks."""
    identity(sha, SHA)
    if not SAFE_PATH.fullmatch(prefix):
        raise Blocked("Превышена разрешённая область снимка.")
    entries = git("ls-tree", "-r", "-z", sha, "--", prefix).split(b"\0")[:-1]
    if not entries or len(entries) > 100:
        raise Blocked("Папка снимка пуста или превышен лимит файлов.")
    total = 0
    directory.mkdir()
    directory.chmod(0o755)
    for entry in entries:
        meta, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id = meta.decode().split()
        path = raw_path.decode()
        if mode not in {"100644", "100755"} or kind != "blob" or not path.startswith(prefix + "/"):
            raise Blocked("Снимок содержит недопустимый тип файла.")
        relative = path.removeprefix(prefix + "/")
        if not SAFE_PATH.fullmatch(relative):
            raise Blocked("Снимок содержит недопустимое имя.")
        total += int(git("cat-file", "-s", object_id))
        if total > MAX_EXPORT:
            raise Blocked("Превышен размер снимка.")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent
        while parent != directory:
            parent.chmod(0o755)
            parent = parent.parent
        target.write_bytes(git("cat-file", "blob", object_id))
        target.chmod(0o644)


@dataclass(frozen=True)
class Evidence:
    check: str
    sha: str
    status: str
    exit_code: int | None
    log_bytes: int
    log_sha256: str
    diagnostics: tuple = ()

    def data(self):
        evidence = asdict(self)
        del evidence["diagnostics"]
        return evidence


def diagnostics(logs):
    labels = ("FAIL:", "ERROR:", "AssertionError:", "SyntaxError:", "TimeoutError:")
    return tuple(line.strip()[:160] for line in logs.decode(errors="replace").splitlines()
                 if line.lstrip().startswith(labels))[:3]


def docker(*args):
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise Blocked("Docker недоступен или отклонил настройки изоляции.") from None


def collect(process, deadline):
    output = bytearray()
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0 or not select.select([process.stdout], [], [], remain)[0]:
            return "timeout", None, output
        chunk = os.read(process.stdout.fileno(), min(4096, MAX_LOG + 1 - len(output)))
        if not chunk:
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return "timeout", None, output
            return ("pass" if code == 0 else "fail"), code, output
        output.extend(chunk)
        if len(output) > MAX_LOG:
            return "output-limit", None, output[:MAX_LOG]


class DockerSandbox:
    def __init__(self, image, project, base):
        self.image = identity(docker("image", "inspect", "--format", "{{.Id}}", image), IMAGE)
        self.project, self.base = project, base

    def run(self, check, sha):
        with TemporaryDirectory(prefix="coordinator-v2-") as area:
            area = Path(area)
            materialize(sha, self.project.source, area / "case")
            materialize(self.base, self.project.harness, area / "harness")
            return self._run(check, sha, area)

    def _run(self, check, sha, area):
        name = f"coordinator-v2-{uuid4().hex}"
        try:
            container = docker(
                "create", "--name", name, "--pull", "never", "--network", "none",
                "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "128",
                "--cpus", "1", "--memory", "768m", "--memory-swap", "768m",
                "--shm-size", "128m", "--log-driver", "none",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=96m,mode=1777",
                "--workdir", "/case", "--env", "HOME=/tmp", "--env", "CI=1",
                *(value for name in PROXY_ENV for value in ("--env", f"{name}=")),
                "--mount", f"type=bind,source={area / 'case'},target=/case,readonly",
                "--mount", f"type=bind,source={area / 'harness'},target=/harness,readonly",
                "--entrypoint", check.argv[0], self.image, *check.argv[1:],
            )
            with subprocess.Popen(
                ["docker", "start", "--attach", container],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            ) as process:
                try:
                    status, code, logs = collect(process, time.monotonic() + check.timeout)
                finally:
                    if process.poll() is None:
                        process.kill()
            # Docker uses these statuses for infrastructure failures. An application
            # can choose the same codes only to block its own acceptance.
            if code is not None:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", container))
                if state["Status"] != "exited" or state["Error"] or state["OOMKilled"] or code in {125, 126, 127}:
                    status = "infrastructure"
            return Evidence(check.name, sha, status, code, len(logs), hashlib.sha256(logs).hexdigest(),
                            diagnostics(logs) if status == "fail" else ())
        except OSError:
            raise Blocked("Docker не запустил проверку.") from None
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
