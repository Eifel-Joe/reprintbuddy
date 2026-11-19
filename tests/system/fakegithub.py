"""A stand-in for GitHub's release list and downloads, for the end-to-end run (tests/system/README.md).

In the container `github.com` and `api.github.com` point to 127.0.0.1 (`/etc/hosts`), and a certificate authority
made for the run is trusted there; nothing in Reprintbuddy knows about it. This server answers exactly what the
installer, the update helper and the pages ask (Spec 11.3a "Paket je Release"):

- `GET https://api.github.com/repos/<owner>/<name>/releases?per_page=30`: the releases of `releases.json` (written by
  `packages.py`), published, with package and checksum as uploaded assets and GitHub's `digest` of the package;
- `GET https://github.com/<owner>/<name>/releases/download/<tag>/<file>`: the file from `<root>/<tag>/`.

Any other request gets 404 and is written to the log as UNEXPECTED; the run fails on it at its end. Standard library
only: it runs on the Python of the container, before anything is installed there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOSTS = ("github.com", "api.github.com")
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class FakeGitHub:
    def __init__(self, root: Path, repo: str, *, context: ssl.SSLContext, log: Path) -> None:
        self.root = root
        self.repo = repo
        self.context = context
        self.log = log
        self.unexpected: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def _record(self, line: str, *, unexpected: bool) -> None:
        with self._lock:
            if unexpected:
                self.unexpected.append(line)
            with self.log.open("a", encoding="utf-8") as file:
                file.write(("UNEXPECTED " if unexpected else "") + line + "\n")

    def releases(self) -> list[dict[str, object]]:
        listed = json.loads((self.root / "releases.json").read_text(encoding="utf-8"))
        found = []
        for item in listed:
            tag = item["tag"]
            assets = []
            for name in (f"reprintbuddy-{tag}.tar.gz", f"reprintbuddy-{tag}.tar.gz.sha256"):
                data = (self.root / tag / name).read_bytes()
                assets.append(
                    {
                        "name": name,
                        "state": "uploaded",
                        "size": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "browser_download_url": f"https://github.com/{self.repo}/releases/download/{tag}/{name}",
                    }
                )
            found.append(
                {
                    "tag_name": tag,
                    "name": tag,
                    "draft": False,
                    "prerelease": bool(item["prerelease"]),
                    "published_at": item["published_at"],
                    "body": item["body"],
                    "assets": assets,
                }
            )
        return found

    def answer(self, host: str, target: str) -> tuple[int, str, bytes]:
        """(status, content type, body) for a GET of `target` on `host`."""
        parts = urlsplit(target)
        if host == "api.github.com" and parts.path == f"/repos/{self.repo}/releases":
            return 200, "application/json", json.dumps(self.releases()).encode()
        prefix = f"/{self.repo}/releases/download/"
        if host == "github.com" and parts.path.startswith(prefix) and not parts.query:
            tag, _, name = parts.path[len(prefix):].partition("/")
            if _NAME.match(tag) and _NAME.match(name) and ".." not in (tag, name) and name.startswith(f"reprintbuddy-{tag}."):
                path = self.root / tag / name
                if path.is_file():
                    return 200, "application/octet-stream", path.read_bytes()
        return 404, "text/plain", b"Not Found"

    def start(self, *, bind: str = "127.0.0.1", port: int = 443) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                status, kind, body = fake.answer(host, self.path)
                fake._record(f"GET {host}{self.path} {status}", unexpected=status != 200)
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _refuse(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))  # else it is read as the next request
                fake._record(f"{self.command} {self.headers.get('Host')}{self.path}", unexpected=True)
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _refuse  # noqa: N815

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's name
                pass  # the log file says what came

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self._server.socket = self.context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-github", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="the folder packages.py wrote")
    parser.add_argument("--repo", required=True, help="owner/name the installation was given")
    parser.add_argument("--cert", type=Path, required=True, help="PEM file: private key and certificate chain")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=443)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(args.cert)
    fake = FakeGitHub(args.root, args.repo, context=context, log=args.log)
    fake.start(bind=args.bind, port=args.port)
    print(f"fake GitHub on {args.bind}:{fake.port}", flush=True)
    threading.Event().wait()  # until the process is ended
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
