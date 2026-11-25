import json
import os
from pathlib import Path
import subprocess


assert os.getuid() != 0
assert not ({"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "DOCKER_HOST"} & set(os.environ))
assert not Path("/var/run/docker.sock").exists()
assert not Path("/case/.git").exists()
assert not any("00000000" == line.split()[1] for line in Path("/proc/net/route").read_text().splitlines()[1:])
for target in ("/case/overwrite", "/harness/overwrite", "/opt/checks/overwrite"):
    try:
        Path(target).write_text("isolation probe")
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected write access to {target}")

result = subprocess.run(
    ["python3", "-I", "-B", "/case/store.py"], input=json.dumps({
        "items": [{"name": "pencil", "category": "office"}, {"name": "cup", "category": "kitchen"}],
        "category": "office",
    }), capture_output=True, text=True, check=True, timeout=5,
)
assert json.loads(result.stdout) == [{"name": "pencil", "category": "office"}]
