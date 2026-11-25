import hashlib
import json
import re
from pathlib import PurePosixPath
from .common import git

MAX_FILE = 100_000
MAX_TOTAL = 240_000

def digest(data):
    return hashlib.sha256(data).hexdigest()

def blob(sha, path):
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        raise ValueError("Unsafe evidence path")
    mode = git("ls-tree", sha, "--", path).decode().split(" ")[0]
    if mode != "100644" and mode != "100755":
        raise ValueError("Evidence must be a regular tracked file")
    if int(git("cat-file", "-s", f"{sha}:{path}")) > MAX_FILE:
        raise ValueError("Evidence file exceeds size limit")
    return git("show", f"{sha}:{path}")

def collect(request, context):
    sha = request["target_sha"]
    raw = blob(sha, request["evidence_manifest_path"])
    manifest = json.loads(raw)
    if set(manifest) != {"items"} or not isinstance(manifest["items"], list) or len(manifest["items"]) > 30:
        raise ValueError("Manifest must contain at most 30 items")
    evidence = []
    ids = set()
    for item in manifest["items"]:
        if set(item) != {"id", "path", "kind", "source_sha"}:
            raise ValueError("Invalid evidence fields")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", item["id"]) or item["id"] in ids:
            raise ValueError("Invalid or duplicate evidence ID")
        ids.add(item["id"])
        data = blob(sha, item["path"])
        status = "available" if item["source_sha"] in (sha, "target") else "stale"
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content, status = None, "unsupported_binary"
        evidence.append({**item, "source_sha":sha if item["source_sha"] == "target" else item["source_sha"],
                         "sha256":digest(data), "status":status, "content":content})
    packet = {"request":request, "context":context, "evidence":evidence,
              "manifest_sha256":digest(raw),
              "commit_history":git("log", "-30", "--format=%H %aI %cI", sha).decode(),
              "history_note":"Последние 30 коммитов не доказывают время всех коммитов; T2 требует полного аудита."}
    if len(json.dumps(packet, ensure_ascii=False).encode()) > MAX_TOTAL:
        raise ValueError("Evidence packet exceeds size limit; select fewer files")
    return packet
