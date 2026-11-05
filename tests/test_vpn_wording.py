"""Spec 3.4: not directly on the internet; remote access through a VPN into the home network."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "src" / "reprintbuddy" / "locales"
TEMPLATES = ROOT / "src" / "reprintbuddy" / "web" / "templates"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_no_text_still_says_home_network_only():
    old = re.compile(r"home network only|nur für dein Heimnetz|designed for a home network|für das Heimnetz gedacht", re.I)
    files = [ROOT / "README.md", ROOT / "README.de.md", ROOT / "SECURITY.md", *LOCALES.glob("*.json"), *TEMPLATES.glob("*.html")]
    for path in files:
        assert not old.search(flat(read(path))), path.name


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "README.md",
            [
                "reachable from outside only through a VPN",
                "Do not expose Reprintbuddy directly to the internet (no port forwarding, no public reverse proxy). "
                "For remote access, use a VPN into your network (e.g. your router's VPN, WireGuard or Tailscale).",
                "HTTP is fine inside your home network or over a VPN.",
                "open Reprintbuddy by its IP address (or a local name); names from public domains, "
                "e.g. VPN DNS names, are accepted only after setup.",
            ],
        ),
        (
            "README.de.md",
            [
                "von außen nur über ein VPN erreichbar",
                "Reprintbuddy nicht direkt ins Internet stellen (keine Portweiterleitung, kein öffentlicher Reverse-Proxy). "
                "Für den Zugriff von unterwegs ein VPN in dein Netz nutzen (z. B. das VPN deines Routers, WireGuard oder Tailscale).",
                "HTTP genügt im Heimnetz oder über ein VPN.",
                "öffne Reprintbuddy über seine IP-Adresse (oder einen lokalen Namen); Namen aus öffentlichen Domains, "
                "z. B. VPN-DNS-Namen, werden erst nach der Einrichtung akzeptiert.",
            ],
        ),
        (
            "SECURITY.md",
            [
                "must not be exposed directly to the internet (no port forwarding, no public reverse proxy); use a VPN for remote access",
                "darf nicht direkt ins Internet gestellt werden (keine Portweiterleitung, kein öffentlicher Reverse-Proxy); für den Zugriff von unterwegs ein VPN nutzen",
            ],
        ),
    ],
)
def test_readme_and_security_recommend_a_vpn(name, expected):
    text = flat(read(ROOT / name))
    for sentence in expected:
        assert sentence in text, (name, sentence)
