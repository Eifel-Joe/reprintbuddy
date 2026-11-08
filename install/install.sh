#!/usr/bin/env bash
# Reprintbuddy installer for Debian 13 or newer (native install, no Docker, systemd).
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/main/install/install.sh)"
#
# Run as root on the system that is to run Reprintbuddy. Safe to run again: the installed version and the
# database stay as they are; packages, user, permissions, units and the service are
# repaired. Versions change only through the update on the settings page.
#
# Options: --language en|de   --repo URL   --dry-run   -h|--help
set -Eeuo pipefail

# Replaced when the repository is published; the script refuses to run with the placeholder.
REPO_URL_DEFAULT="https://github.com/Eifel-Joe/reprintbuddy.git"

APP_USER="reprintbuddy"
INSTALL_DIR="/opt/reprintbuddy"
DATA_DIR="/var/lib/reprintbuddy"
DB_FILE="${DATA_DIR}/reprintbuddy.db"
VENV_DIR="${INSTALL_DIR}/.venv"
SERVICE="reprintbuddy.service"

DRY_RUN=0
LANGUAGE_CHOICE=""
REPO_URL="${REPRINTBUDDY_REPO:-${REPO_URL_DEFAULT}}"
# Test hook: where existing files are looked for. Only allowed together with --dry-run.
DESTDIR="${REPRINTBUDDY_DESTDIR:-}"
DESTDIR="${DESTDIR%/}"
NEW_DATABASE=0
STAGE_DIR=""

cleanup() {
    if [ -n "${STAGE_DIR}" ]; then
        rm -rf "${STAGE_DIR}"
    fi
}
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
die() {
    local code="$1"
    shift
    printf '%s\n' "$@" >&2
    exit "${code}"
}

usage() {
    cat <<'EOF'
Usage: install.sh [--language en|de] [--repo URL] [--dry-run] [-h|--help]

  --language en|de   interface language (default: ask, or en without a terminal)
  --repo URL         git repository to install from (or set REPRINTBUDDY_REPO)
  --dry-run          print what would be changed, change nothing
EOF
}

# Run a command, or only show it in a dry run.
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '+'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@" </dev/null # nothing here is interactive: no command may read from the terminal
    fi
}

# git, venv, pip and the command line run as the service user: the service must be able to
# update itself later, and git refuses a repository owned by somebody else.
# `setsid --wait` gives the command a session without a controlling terminal: a process of the
# less privileged user cannot push keystrokes into root's terminal (TIOCSTI).
as_service_user() {
    run setsid --wait runuser -u "${APP_USER}" -- env "HOME=${DATA_DIR}" "$@"
}

# The same, but the output is wanted (reading commands also run in a dry run).
capture_as_service_user() {
    setsid --wait runuser -u "${APP_USER}" -- env "HOME=${DATA_DIR}" "$@" </dev/null
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --language)
                [ "$#" -ge 2 ] || die 2 "Missing value for --language."
                LANGUAGE_CHOICE="$2"
                shift 2
                ;;
            --repo)
                [ "$#" -ge 2 ] || die 2 "Missing value for --repo."
                REPO_URL="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                die 2 "Unknown option: $1"
                ;;
        esac
    done
    case "${LANGUAGE_CHOICE}" in
        "" | en | de) ;;
        *) die 2 "Unknown language: ${LANGUAGE_CHOICE} (use en or de)." ;;
    esac
    if [ -n "${DESTDIR}" ] && [ "${DRY_RUN}" -ne 1 ]; then
        die 2 "REPRINTBUDDY_DESTDIR is only allowed with --dry-run."
    fi
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            say "Note: not running as root; this is a dry run, so nothing is changed."
        else
            die 1 "Run this script as root."
        fi
    fi
}

# Reprintbuddy needs Python 3.12 or newer. Debian 12 only has 3.11 in its own sources (main; there is
# no python3.12 in bookworm-backports either), so Debian 13 or newer is the floor (v0.3.9 p101).
NEEDS_DEBIAN_MESSAGE="Reprintbuddy needs Debian 13 or newer: it needs Python 3.12 or newer, and Debian 12 comes with Python 3.11. On Debian 12, upgrade the system to Debian 13 (the Debian release upgrade), then run this installer again; it keeps your data."

require_debian() {
    local release="${DESTDIR}/etc/os-release" id_ version
    if [ ! -r "${release}" ]; then
        die 1 "${NEEDS_DEBIAN_MESSAGE}"
    fi
    id_="$(
        # shellcheck disable=SC1090
        . "${release}"
        printf '%s' "${ID:-}"
    )"
    version="$(
        # shellcheck disable=SC1090
        . "${release}"
        printf '%s' "${VERSION_ID:-}"
    )"
    case "${version}" in
        '' | *[!0-9]*) version=0 ;;
    esac
    if [ "${id_}" != "debian" ] || [ "${version}" -lt 13 ]; then
        die 1 "${NEEDS_DEBIAN_MESSAGE}"
    fi
}

# The python3 that will build the virtual environment must be 3.12 or newer, whatever the system
# claims to be. Checked before anything is created. Without python3 (a dry run on a bare system)
# there is nothing to ask.
require_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            return 0
        fi
        die 1 "python3 was not found after installing the packages."
    fi
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        local found
        found="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        die 1 "Reprintbuddy needs Python 3.12 or newer, but python3 here is Python ${found:-of an unknown version}." \
            "Use Debian 13 or newer. On Debian 12, upgrade the system to Debian 13, then run this installer again."
    fi
}

choose_language() {
    if [ -n "${LANGUAGE_CHOICE}" ]; then
        return 0
    fi
    if [ -e "${DESTDIR}${DB_FILE}" ]; then
        return 0 # a re-run keeps the database; the language is only used to create it
    fi
    LANGUAGE_CHOICE="en"
    if [ -t 0 ]; then
        local answer
        while true; do
            printf 'Language for Reprintbuddy [en/de] (default: en): '
            read -r answer || answer=""
            case "${answer}" in
                "") break ;;
                en | de)
                    LANGUAGE_CHOICE="${answer}"
                    break
                    ;;
            esac
        done
    fi
}

# The update helper and this script both change the installation: never at the same time.
refuse_during_update() {
    local state
    state="$(systemctl show --property=ActiveState --value reprintbuddy-update.service 2>/dev/null || true)"
    case "${state}" in
        active | activating | reloading | deactivating)
            die 1 "An update is running. Run the installer again when it has finished."
            ;;
    esac
}

require_repo_url() {
    case "${REPO_URL}" in
        *"<owner>"*) die 1 "Repository URL not set: use --repo or REPRINTBUDDY_REPO." ;;
    esac
}

install_packages() {
    run apt-get update
    run apt-get install -y --no-install-recommends python3 python3-venv git curl sudo ca-certificates
}

create_user() {
    if ! id -u "${APP_USER}" >/dev/null 2>&1; then
        run useradd --system --user-group --home-dir "${DATA_DIR}" --no-create-home \
            --shell /usr/sbin/nologin "${APP_USER}"
    fi
}

prepare_dirs() {
    local existing="${DESTDIR}${INSTALL_DIR}"
    run mkdir -p "${DATA_DIR}"
    run chown "${APP_USER}:${APP_USER}" "${DATA_DIR}"
    run chmod 700 "${DATA_DIR}"
    # Repair files that ended up with another owner (a manual command run as root, for example).
    run find "${DATA_DIR}" -xdev ! -user "${APP_USER}" -exec chown -h "${APP_USER}:${APP_USER}" {} +
    if [ -d "${existing}" ] && [ ! -e "${existing}/.git" ] && [ -n "$(ls -A "${existing}")" ]; then
        die 1 "${INSTALL_DIR} exists but is not a Reprintbuddy installation."
    fi
    run mkdir -p "${INSTALL_DIR}"
    run chown -R "${APP_USER}:${APP_USER}" "${INSTALL_DIR}"
}

latest_release_tag() {
    # `tail -n 1` instead of `head -n 1`: head can end early and kill the pipe under pipefail.
    capture_as_service_user git -C "${INSTALL_DIR}" tag --list 'v*' |
        grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' | sort -V | tail -n 1
}

checkout_newest_release() {
    local tag="vX.Y.Z"
    if [ "${DRY_RUN}" -ne 1 ]; then
        tag="$(latest_release_tag || true)"
        [ -n "${tag}" ] || die 1 "No release found in the repository."
    fi
    as_service_user git -C "${INSTALL_DIR}" checkout --quiet --detach "${tag}"
}

fetch_code() {
    if [ -e "${DESTDIR}${INSTALL_DIR}/.git" ]; then
        # A first run that stopped after the clone (for example "No release found" before the
        # first tag existed) leaves the default branch checked out and no database behind.
        # Nothing is installed yet then, so move to the release instead of keeping that state.
        if [ ! -e "${DESTDIR}${DB_FILE}" ] &&
            ! capture_as_service_user git -C "${INSTALL_DIR}" describe --tags --exact-match HEAD >/dev/null 2>&1; then
            say "Unfinished installation found, switching to the newest release."
            as_service_user git -C "${INSTALL_DIR}" fetch --prune --prune-tags --force --tags origin
            checkout_newest_release
            return 0
        fi
        say "Existing installation found, keeping version."
        return 0
    fi
    as_service_user git clone "${REPO_URL}" "${INSTALL_DIR}"
    checkout_newest_release
}

# Does the installed environment still fit? It was built with one Python and holds its packages under that
# Python's lib/python3.x; its `bin/python` is a link to the system's python3. After a release upgrade of
# Debian the system's python3 is another one (3.11 -> 3.13): the link is alive, but the packages are not found,
# and the service does not start. The environment is of no use when it was built with another Python than the
# system's now, with a Python older than 3.12, or when python or pip is missing in it. Only the environment is
# rebuilt, never the data (v0.3.9 p101). Its Python version is read from pyvenv.cfg: the environment's own
# python is not run (as root) just to ask.
python_version_of_system() {
    python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

venv_usable() {
    local venv_python="${DESTDIR}${VENV_DIR}/bin/python" venv_pip="${DESTDIR}${VENV_DIR}/bin/pip"
    local cfg="${DESTDIR}${VENV_DIR}/pyvenv.cfg" version major minor system
    [ -x "${venv_python}" ] && [ -x "${venv_pip}" ] && [ -r "${cfg}" ] || return 1
    version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "${cfg}" | head -n 1)"
    major="${version%%.*}"
    minor="${version#*.}"
    minor="${minor%%.*}"
    case "${major}${minor}" in
        '' | *[!0-9]*) return 1 ;;
    esac
    { [ "${major}" -gt 3 ] || { [ "${major}" -eq 3 ] && [ "${minor}" -ge 12 ]; }; } || return 1
    system="$(python_version_of_system)"
    [ -z "${system}" ] || [ "${system}" = "${major}.${minor}" ]
}

install_python() {
    local rebuild=0
    if [ -d "${DESTDIR}${VENV_DIR}" ] && ! venv_usable; then
        say "The Python environment does not fit the system's Python any more (for example after a system"
        say "upgrade): rebuilding it. The installed version, the database and the settings are kept."
        rebuild=1
        run systemctl stop "${SERVICE}" || true
        run rm -rf "${VENV_DIR}" "${INSTALL_DIR}/.venv.rollback"
    fi
    if [ "${rebuild}" -eq 1 ] || [ ! -d "${DESTDIR}${VENV_DIR}" ]; then
        as_service_user python3 -m venv "${VENV_DIR}"
    fi
    run rm -rf "${INSTALL_DIR}/build" # old build leftovers would bring back deleted modules
    as_service_user "${VENV_DIR}/bin/python" -m pip install --no-cache-dir --upgrade "${INSTALL_DIR}"
}

init_database() {
    if [ -e "${DESTDIR}${DB_FILE}" ]; then
        say "Existing database kept."
        return 0
    fi
    NEW_DATABASE=1
    as_service_user "${VENV_DIR}/bin/reprintbuddy" init --db "${DB_FILE}" --language "${LANGUAGE_CHOICE}"
}

# Written from this script, never copied from ${INSTALL_DIR}: that tree belongs to the
# service user, and root must not install a sudoers rule, a unit or a wrapper from a place
# the (web-facing) service user can write to. The copies in install/ are kept equal by a test.
write_system_files() {
    cat >"$1/reprintbuddy.sudoers" <<'RB_SUDOERS_EOF'
# Lets the Reprintbuddy service restart itself, stop and start itself (for a rollback) and
# start its update unit; nothing else.
reprintbuddy ALL=(root) NOPASSWD: /usr/bin/systemctl restart --no-block reprintbuddy.service, /usr/bin/systemctl restart reprintbuddy.service, /usr/bin/systemctl start --no-block reprintbuddy-update.service, /usr/bin/systemctl stop reprintbuddy.service, /usr/bin/systemctl start reprintbuddy.service
RB_SUDOERS_EOF
    cat >"$1/reprintbuddy.service" <<'RB_SERVICE_EOF'
[Unit]
Description=Reprintbuddy
After=network-online.target
Wants=network-online.target

[Service]
User=reprintbuddy
Group=reprintbuddy
WorkingDirectory=/var/lib/reprintbuddy
ExecStart=/opt/reprintbuddy/.venv/bin/reprintbuddy serve --db /var/lib/reprintbuddy/reprintbuddy.db
Restart=on-failure
RestartSec=5
UMask=0077
# Ports below 1024 (for example 443) without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
RB_SERVICE_EOF
    cat >"$1/reprintbuddy-update.service" <<'RB_UPDATE_SERVICE_EOF'
[Unit]
Description=Reprintbuddy update (started from the settings page)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=reprintbuddy
Group=reprintbuddy
WorkingDirectory=/var/lib/reprintbuddy
ExecStart=/opt/reprintbuddy/.venv/bin/reprintbuddy update --db /var/lib/reprintbuddy/reprintbuddy.db
TimeoutStartSec=30min
UMask=0077
Environment=HOME=/var/lib/reprintbuddy
RB_UPDATE_SERVICE_EOF
    cat >"$1/reprintbuddy-cli.sh" <<'RB_CLI_EOF'
#!/usr/bin/env bash
# /usr/local/bin/reprintbuddy: run the command line as the service user, so that
# database files created or changed here keep their owner.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    cd /
    if [ "${1:-}" = "set-password" ] || [ "${1:-}" = "restore-backup" ]; then
        # These read a password from the terminal, so they need the terminal.
        exec runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy /opt/reprintbuddy/.venv/bin/reprintbuddy "$@"
    fi
    # Everything else needs no terminal. A new session without one keeps the less privileged
    # process from pushing keystrokes into root's terminal (TIOCSTI).
    exec setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy /opt/reprintbuddy/.venv/bin/reprintbuddy "$@" </dev/null
fi
exec /opt/reprintbuddy/.venv/bin/reprintbuddy "$@"
RB_CLI_EOF
}

# Written once into a private folder; checked against the installed code, then installed.
stage_system_files() {
    if [ -z "${STAGE_DIR}" ]; then
        STAGE_DIR="$(mktemp -d)"
        write_system_files "${STAGE_DIR}"
    fi
}

# This script comes from the branch the user fetched it from, the code from a release tag.
# If the two disagree about the units, the sudoers rule or the wrapper, installing this
# script's copies would put files on the system that the installed version does not expect.
check_installer_matches_code() {
    stage_system_files
    local file tree tag
    for file in reprintbuddy.sudoers reprintbuddy.service reprintbuddy-update.service reprintbuddy-cli.sh; do
        tree="${DESTDIR}${INSTALL_DIR}/install/${file}"
        if [ ! -e "${tree}" ] && [ "${DRY_RUN}" -eq 1 ]; then
            continue # a dry run that cloned nothing has nothing to compare with
        fi
        if ! cmp -s "${STAGE_DIR}/${file}" "${tree}" 2>/dev/null; then
            tag="$(capture_as_service_user git -C "${INSTALL_DIR}" describe --tags --exact-match HEAD 2>/dev/null || true)"
            die 1 "The installer does not match the installed version (${file} differs)." \
                "Use the install.sh of the tag ${tag:-of the installed version} (${REPO_URL}), for example:" \
                "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/${tag:-<tag>}/install/install.sh)\""
        fi
    done
}

install_system_files() {
    stage_system_files
    # Checked before it is installed: a broken sudoers file can lock root out of sudo.
    run visudo -cf "${STAGE_DIR}/reprintbuddy.sudoers"
    run install -m 0440 -o root -g root "${STAGE_DIR}/reprintbuddy.sudoers" /etc/sudoers.d/reprintbuddy
    run install -m 0644 -o root -g root "${STAGE_DIR}/reprintbuddy.service" /etc/systemd/system/reprintbuddy.service
    run install -m 0644 -o root -g root "${STAGE_DIR}/reprintbuddy-update.service" \
        /etc/systemd/system/reprintbuddy-update.service
    run install -m 0755 -o root -g root "${STAGE_DIR}/reprintbuddy-cli.sh" /usr/local/bin/reprintbuddy
}

start_service() {
    run systemctl daemon-reload
    run systemctl enable "${SERVICE}"
    run systemctl restart "${SERVICE}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        return 0
    fi
    local url waited=0
    url="$(capture_as_service_user "${VENV_DIR}/bin/reprintbuddy" url --db "${DB_FILE}")"
    while [ "${waited}" -lt 30 ]; do
        if curl -fsSk --max-time 5 -o /dev/null "${url}/healthz"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    die 1 "Reprintbuddy did not start. See: journalctl -u reprintbuddy -n 50"
}

print_next_steps() {
    local ip="<container-ip>" url
    if [ "${DRY_RUN}" -eq 1 ]; then
        url="http://${ip}:8080/"
        if [ "${NEW_DATABASE}" -eq 1 ]; then
            url="http://${ip}:8080/setup"
        fi
    else
        ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
        ip="${ip:-<container-ip>}"
        url="$(capture_as_service_user "${VENV_DIR}/bin/reprintbuddy" url --db "${DB_FILE}" --host "${ip}" --next)"
    fi
    case "${url}" in
        */setup)
            say "Finish the setup now: ${url}"
            say "Until the admin account exists, anyone in your network can create it."
            ;;
        *)
            say "Reprintbuddy is running: ${url}"
            ;;
    esac
}

main() {
    cd /
    export DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0
    parse_args "$@"
    require_root
    require_debian
    require_repo_url
    refuse_during_update
    choose_language
    install_packages
    require_python
    create_user
    prepare_dirs
    fetch_code
    check_installer_matches_code
    install_python
    init_database
    install_system_files
    start_service
    print_next_steps
}

main "$@"
