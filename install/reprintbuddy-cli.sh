#!/usr/bin/env bash
# /usr/local/bin/reprintbuddy: run the command line as the service user, so that
# database files created or changed here keep their owner.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    cd /
    if [ "${1:-}" = "set-password" ]; then
        # Reads the password from the terminal, so it needs the terminal.
        exec runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy /opt/reprintbuddy/.venv/bin/reprintbuddy "$@"
    fi
    # Everything else needs no terminal. A new session without one keeps the less privileged
    # process from pushing keystrokes into root's terminal (TIOCSTI).
    exec setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy /opt/reprintbuddy/.venv/bin/reprintbuddy "$@" </dev/null
fi
exec /opt/reprintbuddy/.venv/bin/reprintbuddy "$@"
