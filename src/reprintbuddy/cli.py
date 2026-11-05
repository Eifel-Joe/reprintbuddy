"""Command line: serve, migrate, init, set-password, set-port.

Every command takes `--db PATH`; the database path is the only thing that
does not live in the database itself (Spec 11.5a).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import uvicorn

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.netcheck import can_bind
from reprintbuddy.db import DEFAULT_DB_PATH, make_engine, make_sessionmaker, upgrade
from reprintbuddy.web.app import create_app


def _error(message: str) -> int:
    print(f"reprintbuddy: {message}", file=sys.stderr)
    return 2


def _session(db: Path):
    engine = make_engine(db)
    upgrade(engine)
    return make_sessionmaker(engine)()


@contextlib.contextmanager
def _pem_files(cert: str, key: str) -> Iterator[tuple[str, str]]:
    """Write certificate and key to private temp files; always remove them."""
    paths: list[str] = []
    try:
        for content in (cert, key):
            fd, path = tempfile.mkstemp(suffix=".pem", prefix="reprintbuddy-")
            paths.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            if os.name == "posix":
                os.chmod(path, 0o600)
        yield paths[0], paths[1]
    finally:
        for path in paths:
            with contextlib.suppress(OSError):
                os.remove(path)


def _serve(args: argparse.Namespace) -> int:
    app = create_app(args.db)
    with app.state.sessionmaker() as session:
        host = str(st.get_setting(session, "host"))
        port = int(st.get_setting(session, "port"))
        tls = st.tls_enabled(session)
        cert = st.get_setting(session, "tls_cert")
        key = st.get_setting(session, "tls_key")
    if tls:
        with _pem_files(str(cert), str(key)) as (cert_file, key_file):
            uvicorn.run(app, host=host, port=port, ssl_certfile=cert_file, ssl_keyfile=key_file)
    else:
        uvicorn.run(app, host=host, port=port)
    return 0


def _migrate(args: argparse.Namespace) -> int:
    with _session(args.db):
        pass
    return 0


def _init(args: argparse.Namespace) -> int:
    with _session(args.db) as session:
        st.set_setting(session, "language", args.language)
    return 0


def _set_password(args: argparse.Namespace) -> int:
    try:
        username = auth.clean_username(args.username)
    except ValueError:
        return _error("the user name must not be empty")
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        return _error("the two passwords do not match")
    with _session(args.db) as session:
        try:
            auth.set_password(session, username, password)
        except ValueError:
            return _error(f"the password must have at least {auth.MIN_PASSWORD_LENGTH} characters")
    print(f"Password set for {username}. All sessions have ended.")
    return 0


def _set_port(args: argparse.Namespace) -> int:
    try:
        port = int(args.port)
        st.validate("port", port)
    except ValueError:
        return _error("the port must be a number from 1 to 65535")
    with _session(args.db) as session:
        st.set_setting(session, "port", port)
    print(f"Port set to {port}. Restart Reprintbuddy for it to take effect.")
    if port < 1024:
        print(
            "Note: ports below 1024 need extra rights and the service does not run as root, "
            "so it probably cannot use this port. If it does not start, choose a higher one."
        )
    return 0


def _set_host(args: argparse.Namespace) -> int:
    host = args.host.strip()
    if not host or not can_bind(host):
        return _error("host: this machine cannot listen on that address")
    with _session(args.db) as session:
        st.set_setting(session, "host", host)
    print(f"Listen address set to {host}. Restart Reprintbuddy for it to take effect.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reprintbuddy")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler, help_: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_)
        sub.add_argument(
            "--db",
            type=Path,
            default=DEFAULT_DB_PATH,
            help=f"database file (default: {DEFAULT_DB_PATH.as_posix()})",
        )
        sub.set_defaults(handler=handler)
        return sub

    command("serve", _serve, "run the web interface")
    command("migrate", _migrate, "create or update the database")
    init = command("init", _init, "migrate and set the interface language (for the installer)")
    init.add_argument("--language", choices=st.LANGUAGES, default="en")
    set_password = command("set-password", _set_password, "set the admin password (emergency exit)")
    set_password.add_argument("--username", required=True)
    set_port = command("set-port", _set_port, "set the listen port (emergency exit)")
    set_port.add_argument("port", help="port number, 1 to 65535")
    set_host = command("set-host", _set_host, "set the listen address (emergency exit)")
    set_host.add_argument("host", help="address to listen on, for example 0.0.0.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.handler(args)
