"""The PoC's Codex exec protocol, with bounded I/O and no raw log export."""

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
from uuid import UUID

MAX_STREAM = 512 * 1024
MAX_ANSWER = 96 * 1024
DISABLED = ("shell_tool", "unified_exec", "hooks", "apps", "plugins", "multi_agent", "multi_agent_v2",
            "view_image", "image_generation", "goals")


class CliError(Exception):
    pass


def validate(value, schema):
    kind = schema["type"]
    if kind == "object" and isinstance(value, dict):
        fields = schema["properties"]
        if set(value) == set(fields):
            for key, child in fields.items():
                validate(value[key], child)
            return
    if kind == "array" and isinstance(value, list):
        for child in value:
            validate(child, schema["items"])
        return
    if kind == "string" and isinstance(value, str) and value in schema.get("enum", [value]):
        return
    raise CliError("invalid_output")


def classify(text, fallback):
    if re.search(r"usage limit|rate limit|quota|too many requests|\b429\b|out of credits", text, re.I):
        return "quota"
    if re.search(r"not logged in|unauthorized|authentication|refresh token|\b401\b|\b403\b", text, re.I):
        return "auth"
    if re.search(r"operation not permitted|sandbox_apply|bwrap|connection refused", text, re.I):
        return "unavailable"
    return fallback


def usage_sample(value):
    if not isinstance(value, dict):
        return None
    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
    sample = {key: value[key] for key in fields if type(value.get(key)) is int and 0 <= value[key] <= 2**53 - 1}
    return sample or None


def parse_turn(stdout, stderr, returncode, answer_path, schema, previous):
    try:
        events = [json.loads(line) for line in stdout.splitlines()]
        if not all(isinstance(event, dict) for event in events):
            raise ValueError
        failures = [event for event in events if event.get("type") in {"turn.failed", "error"}]
        if returncode or failures:
            # Only actual diagnostics are considered. A successful turn may emit cache warnings.
            failure_text = json.dumps(failures) + stderr.decode(errors="replace")
            raise CliError(classify(failure_text, "cli_error"))
        starts = [event for event in events if event.get("type") == "thread.started"]
        turns = [event for event in events if event.get("type") == "turn.completed"]
        if len(starts) != 1 or len(turns) != 1 or events[-1] is not turns[0]:
            raise ValueError
        thread = str(UUID(starts[0]["thread_id"]))
        if previous is not None and thread != previous:
            raise CliError("wrong_session")
        replies = []
        for event in events:
            if event.get("type", "").startswith("item."):
                item = event["item"]
                if item["type"] not in {"reasoning", "agent_message"}:
                    raise ValueError
                if item["type"] == "agent_message" and event["type"] == "item.completed":
                    replies.append(item["text"])
        with answer_path.open("rb") as stream:
            raw = stream.read(MAX_ANSWER + 1)
        if not replies or len(raw) > MAX_ANSWER:
            raise ValueError
        answer = json.loads(raw)
        validate(answer, schema)
        if json.loads(replies[-1]) != answer:
            raise ValueError
        return thread, answer, usage_sample(turns[0].get("usage"))
    except (ValueError, AttributeError, KeyError, TypeError, IndexError, OSError):
        if returncode:
            raise CliError(classify(stderr.decode(errors="replace"), "cli_error")) from None
        raise CliError("invalid_output") from None


def run_bounded(command, cwd, env, stdin, deadline, cancelled):
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise CliError("unavailable") from None
    with process, selectors.DefaultSelector() as poller:
        buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
        for stream in buffers:
            poller.register(stream, selectors.EVENT_READ)
        try:
            while poller.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if cancelled.is_set() or remaining <= 0:
                    raise CliError("uncertain" if cancelled.is_set() else "timeout")
                for key, _ in poller.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 16384)
                    buffer = buffers[key.fileobj]
                    buffer.extend(chunk)
                    if len(buffer) > MAX_STREAM:
                        raise CliError("invalid_output")
                    if not chunk:
                        poller.unregister(key.fileobj)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        return process.returncode, *map(bytes, buffers.values())


class Adapter:
    def __init__(self, binary, timeout=120):
        self.binary, self.timeout = str(Path(binary).resolve()), timeout

    def __call__(self, job, directory, codex_home, previous, cancelled):
        # Keep environment narrow: worker credentials, API keys and endpoint overrides are excluded.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")
               if key in os.environ}
        env["CODEX_HOME"] = str(codex_home)
        deadline = time.monotonic() + self.timeout
        status, stdout, stderr = run_bounded(
            [self.binary, "login", "status"], directory, env, subprocess.DEVNULL, deadline, cancelled)
        if status or b"logged in using chatgpt" not in (stdout + stderr).lower():
            raise CliError("auth")
        with TemporaryDirectory(prefix="turn-", dir=directory) as temp:
            temp = Path(temp)
            answer, schema, prompt = (temp / name for name in ("answer.json", "schema.json", "prompt.txt"))
            schema.write_text(json.dumps(job["schema"]), encoding="utf-8")
            prompt.write_text(job["prompt"], encoding="utf-8")
            command = [self.binary, "exec", "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only"]
            for feature in DISABLED:
                command.extend(("--disable", feature))
            command.extend(("-c", 'approval_policy="never"', "-c", 'web_search="disabled"'))
            if previous:
                command.append("resume")
            command.extend(("--json", "--output-schema", str(schema), "-o", str(answer)))
            if previous:
                command.append(previous)
            command.append("-")
            with prompt.open("rb") as stdin:
                code, stdout, stderr = run_bounded(command, directory, env, stdin, deadline, cancelled)
            return parse_turn(stdout, stderr, code, answer, job["schema"], previous)
