import configparser
from pathlib import Path

from reprintbuddy.ops.service import RESTART_CMD, RESTART_WAIT_CMD, START_UPDATE_CMD

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
DB = "/var/lib/reprintbuddy/reprintbuddy.db"


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep the case of the keys
    parser.read(INSTALL / name, encoding="utf-8")
    return parser


def test_service_unit():
    u = unit("reprintbuddy.service")
    s = u["Service"]
    assert s["User"] == "reprintbuddy"
    assert s["Group"] == "reprintbuddy"
    assert s["ExecStart"] == f"/opt/reprintbuddy/.venv/bin/reprintbuddy serve --db {DB}"
    assert s["WorkingDirectory"] == "/var/lib/reprintbuddy"
    assert s["Restart"] == "on-failure"
    assert s["UMask"] == "0077"
    assert s["AmbientCapabilities"] == "CAP_NET_BIND_SERVICE"
    assert "network-online.target" in u["Unit"]["After"]
    assert u["Install"]["WantedBy"] == "multi-user.target"


def test_update_unit():
    u = unit("reprintbuddy-update.service")
    s = u["Service"]
    assert s["Type"] == "oneshot"
    assert s["User"] == "reprintbuddy"
    assert s["ExecStart"] == f"/opt/reprintbuddy/.venv/bin/reprintbuddy update --db {DB}"
    assert s["TimeoutStartSec"] == "30min"
    assert s["UMask"] == "0077"
    assert not u.has_section("Install")  # started on request only


def test_units_allow_sudo_in_lxc():
    forbidden = (
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectHome",
        "RestrictNamespaces",
        "SystemCallFilter",
    )
    for name in ("reprintbuddy.service", "reprintbuddy-update.service"):
        text = (INSTALL / name).read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, (name, word)


def test_sudoers_matches_python_commands():
    lines = [
        line
        for line in (INSTALL / "reprintbuddy.sudoers").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1
    prefix = "reprintbuddy ALL=(root) NOPASSWD: "
    assert lines[0].startswith(prefix)
    rest = lines[0][len(prefix):]
    assert {c.strip() for c in rest.split(", ")} == {
        " ".join(c[2:]) for c in (RESTART_CMD, RESTART_WAIT_CMD, START_UPDATE_CMD)
    }
    assert "*" not in rest
    assert "ALL" not in rest


def test_install_files_have_lf():
    for path in INSTALL.iterdir():
        if path.is_file():
            assert b"\r" not in path.read_bytes(), path.name
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.sh text eol=lf" in attributes
    assert "install/* text eol=lf" in attributes


def test_cli_wrapper():
    text = (INSTALL / "reprintbuddy-cli.sh").read_text(encoding="utf-8")
    assert "runuser -u reprintbuddy --" in text
    assert "/opt/reprintbuddy/.venv/bin/reprintbuddy" in text
