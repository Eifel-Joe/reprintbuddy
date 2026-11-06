"""Shared helpers for the Bambuddy HTTP tests: address, key and the recorded-shape answers.

The JSON files under `fixtures/` are built from Bambuddy's response schemas and routes;
`fixtures/SOURCES.md` names the source of each one.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from reprintbuddy.bambuddy.client import HttpBambuddyClient

URL = "http://bambuddy.test:8000"
API = f"{URL}/api/v1"
KEY = "bb_testkey"

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> object:
    """A fresh copy of one fixture, so a test may change it."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def client(http=None) -> HttpBambuddyClient:
    """A client for the test address; by default each call opens a plain `httpx.AsyncClient`."""
    return HttpBambuddyClient(URL, KEY, http or (lambda: httpx.AsyncClient()))
