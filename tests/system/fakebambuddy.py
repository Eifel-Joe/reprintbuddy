"""A stand-in for Bambuddy's REST API, for the end-to-end run (tests/system/README.md).

Without a connected Bambuddy every page leads to the Bambuddy setup, the update and backup pages too. This server
lets the setup pass (`GET /api/v1/auth/me`: a user with every required right, no administrator, no live channel)
and answers the readings in the background with an empty printer farm. It only reads, like Reprintbuddy's tests
it never prints anything:

- the token of the live channel (`POST /auth/ws-token`) gets 403, as Bambuddy answers without `websocket:connect`;
- any other write (POST, PUT, PATCH, DELETE) gets 405 and is written to the log as UNEXPECTED; the run fails on it;
- a reading it does not know gets 404 and is written as UNKNOWN; the run shows it but goes on (a new reading of a
  later version is no failure of the update).

Requests without the right key get 401. The key never goes into the log. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

USER = "e2e-reprint"
# The rights of `bambuddy/connection.py`: all required ones and withdrawing; neither the live channel (it would
# open a websocket) nor uploading.
PERMISSIONS = [
    "library:read_all",
    "queue:read_all",
    "queue:create",
    "queue:start_unreviewed",
    "printers:read",
    "inventory:read",
    "filaments:read",
    "settings:read",
    "queue:delete_own",
]

ANSWERS: dict[str, object] = {
    "/api/v1/auth/me": {"username": USER, "is_admin": False, "groups": [{"name": "Reprintbuddy"}], "permissions": PERMISSIONS},
    "/api/v1/printers/": [],
    "/api/v1/bulk/printer-statuses": [],
    "/api/v1/queue/": [],
    "/api/v1/settings/": {"queue_shortest_first": False},
    "/api/v1/spoolman/status": {"enabled": False, "url": None, "connected": False},
    "/api/v1/inventory/spools": [],
    "/api/v1/library/folders/": [],
    "/api/v1/library/files/": [],
}


class FakeBambuddy:
    def __init__(self, key: str, *, log: Path) -> None:
        self.key = key
        self.log = log
        self.unexpected: list[str] = []
        self.unknown: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _record(self, line: str, kind: str = "") -> None:
        with self._lock:
            if kind == "UNEXPECTED":
                self.unexpected.append(line)
            elif kind == "UNKNOWN":
                self.unknown.append(line)
            with self.log.open("a", encoding="utf-8") as file:
                file.write((f"{kind} " if kind else "") + line + "\n")

    def start(self, *, bind: str = "127.0.0.1", port: int = 0) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes = b"", kind: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                path = urlsplit(self.path).path
                if self.headers.get("X-API-Key") != fake.key:
                    fake._record(f"GET {path} 401")
                    self._send(401, b'{"detail":"Not authenticated"}')
                    return
                if path in ANSWERS:
                    fake._record(f"GET {path} 200")
                    self._send(200, json.dumps(ANSWERS[path]).encode())
                    return
                fake._record(f"GET {path}", "UNKNOWN")
                self._send(404, b'{"detail":"Not Found"}')

            def _refuse(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))  # else it is read as the next request
                path = urlsplit(self.path).path
                if self.command == "POST" and path == "/api/v1/auth/ws-token":
                    # the token of the live channel changes nothing; without `websocket:connect` Bambuddy says 403
                    fake._record(f"POST {path} 403")
                    self._send(403, b'{"detail":"Missing permission: websocket:connect"}')
                    return
                fake._record(f"{self.command} {path}", "UNEXPECTED")
                self._send(405, b'{"detail":"Method Not Allowed"}')

            do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's name
                pass

        self._server = ThreadingHTTPServer((bind, port), Handler)
        threading.Thread(target=self._server.serve_forever, name="fake-bambuddy", daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-file", type=Path, required=True, help="file holding the API key the setup enters")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    fake = FakeBambuddy(args.key_file.read_text(encoding="utf-8").strip(), log=args.log)
    fake.start(port=args.port)
    print(f"fake Bambuddy on {fake.url}", flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
