"""Local dashboard for a repository accessible to the signed-in gh account."""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = "cryptospacerus-maker/forY-cassa-"


class Dashboard(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == "/":
            data = Path(__file__).with_name("index.html").read_bytes()
            mime = "text/html; charset=utf-8"
        elif route.path == "/issues":
            page = parse_qs(route.query).get("page", ["1"])[0]
            if not page.isascii() or not page.isdigit() or len(page) > 6:
                return self.send_error(400)
            endpoint = f"repos/{REPO}/issues?state=all&sort=updated&per_page=100&page={page}"
            try:
                data = subprocess.check_output(["gh", "api", endpoint], timeout=25)
                if not isinstance(json.loads(data), list):
                    raise ValueError("Expected issue list")
            except (OSError, ValueError, subprocess.SubprocessError):
                return self.send_error(502, "Check gh auth status and repository access")
            mime = "application/json"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    with HTTPServer(("127.0.0.1", 8765), Dashboard) as server:
        print("http://127.0.0.1:8765", flush=True)
        server.serve_forever()
