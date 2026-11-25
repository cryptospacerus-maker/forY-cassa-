import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "cryptospacerus-maker/forY-cassa-"

def read_json(path):
    return json.loads(Path(path).read_text())

def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, timeout=30)

def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
