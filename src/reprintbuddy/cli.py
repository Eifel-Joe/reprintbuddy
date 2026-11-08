"""Command line: serve, migrate, init, set-password, set-port, set-host, disable-https, restore-backup, update, url.

Every command takes `--db PATH`; the database path is the only thing that
does not live in the database itself (Spec 11.5a).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import signal
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import uvicorn

from reprintbuddy import __version__, auth
from reprintbuddy import settings as st
from reprintbuddy.netcheck import can_bind
from reprintbuddy.ops import restorecmd
from reprintbuddy.ops.context import OpsContext
from reprintbuddy.ops.runner import subprocess_runner
from reprintbuddy.ops.server import base_url, connect_host, effective_from_settings
from reprintbuddy.ops.service import INSTALL_DIR
from reprintbuddy.ops.updater import http_health_waiter, interrupt_handler, run_update
from reprintbuddy.db import DEFAULT_DB_PATH, make_engine, make_sessionmaker, upgrade
from reprintbuddy.web.app import create_app


# Open live streams (GET /live) never end by themselves; without this limit Uvicorn waits
# for them without end when stopping and systemd kills the service only after 90 s.
SHUTDOWN_GRACE_SECONDS = 3


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
        effective = effective_from_settings(session)
        host = str(st.get_setting(session, "host"))
        port = int(st.get_setting(session, "port"))
        tls = st.tls_enabled(session)
        cert = st.get_setting(session, "tls_cert")
        key = st.get_setting(session, "tls_key")
    app.state.ops = OpsContext(effective=effective)
    if tls:
        with _pem_files(str(cert), str(key)) as (cert_file, key_file):
            uvicorn.run(
                app,
                host=host,
                port=port,
                ssl_certfile=cert_file,
                ssl_keyfile=key_file,
                timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
            )
    else:
        uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS)
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
            "Note: ports below 1024 need the right to bind privileged ports. The service set up "
            "by the install script has it; a service started another way may not. "
            "If it does not start after the restart, choose a higher port."
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


def _restore_backup(args: argparse.Namespace) -> int:
    """Restore a full backup from a file while the service is stopped (the emergency way, Spec 11.6)."""
    return restorecmd.restore_backup(
        args.file,
        args.db,
        runner=subprocess_runner,
        # On the terminal only: not an argument, so not in the shell history or the process list.
        ask_password=lambda: getpass.getpass("Password of the backup: "),
        say=print,
        complain=lambda text: print(text, file=sys.stderr),
        now=datetime.now(UTC),
    )


def _update(args: argparse.Namespace) -> int:
    """Update to the newest release (or `--target`); started by the update unit."""
    if not args.db.is_file():
        _error(f"database not found: {args.db}")
        return 1
    signal.signal(signal.SIGTERM, interrupt_handler(args.db, now=lambda: datetime.now(UTC)))
    result = run_update(
        args.db,
        install_dir=INSTALL_DIR,
        runner=subprocess_runner,
        wait_healthy=http_health_waiter(args.db),
        now=lambda: datetime.now(UTC),
        target=args.target,
    )
    if result.state == "succeeded":
        return 0
    return 3 if result.state in ("running", "queued") else 1


def _url(args: argparse.Namespace) -> int:
    """Print the address Reprintbuddy answers on; the install script uses it."""
    if not args.db.is_file():
        _error(f"database not found: {args.db}")
        return 1
    with _session(args.db) as session:
        host = args.host or connect_host(str(st.get_setting(session, "host")))
        port = int(st.get_setting(session, "port"))
        url = base_url(host, port, st.tls_enabled(session))
        if args.next:
            url += "/" if auth.has_user(session) else "/setup"
    print(url)
    return 0


def _disable_https(args: argparse.Namespace) -> int:
    with _session(args.db) as session:
        st.set_setting(session, "tls_cert", None, commit=False)
        st.set_setting(session, "tls_key", None, commit=False)
        session.commit()
    print("HTTPS turned off. Restart Reprintbuddy for it to take effect.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reprintbuddy")
    parser.add_argument("--version", action="version", version=f"reprintbuddy {__version__}")
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
    update = command("update", _update, "update to the newest release (started from the settings page)")
    update.add_argument("--target", help="release tag to update to, for example v0.2.0")
    url = command("url", _url, "print the address of the web interface (for the installer)")
    url.add_argument("--host", help="use this host instead of the saved listen address")
    url.add_argument("--next", action="store_true", help="add the page to open next (/setup until there is an account)")
    command("disable-https", _disable_https, "remove the HTTPS certificate (emergency exit)")
    restore = command(
        "restore-backup", _restore_backup, "restore a full backup while the service is stopped (emergency exit)"
    )
    restore.add_argument("file", type=Path, help="the backup file (.zip) from Settings, Update and backup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.handler(args)
