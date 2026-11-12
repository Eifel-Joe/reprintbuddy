"""Records the screenshots of the documentation (`docs/assets/images/{en,de}/NAME.png`).

The script starts the real application (`reprintbuddy.web.app.create_app`) in a temporary folder with
a fresh database, against the in-memory Bambuddy of the tests (`reprintbuddy.bambuddy.fake`), and
lets a local Chromium browser (Edge or Chrome) take the pictures through the DevTools protocol.
Everything is made up (an "Examples" folder with a few print files, three printers, a handful of
articles); nothing leaves the machine and no network address of a real installation appears.

Call (from the project folder)::

    .venv/bin/python tools/docs_screenshots.py [--out docs/assets/images] [--only NAME ...] [--lang en|de]

(on Windows `.venv/Scripts/python.exe`). Each picture is taken in English and in German: the
language of the interface is the setting `language`, the two folders get the same file names.

Requirements
  * the project's virtual environment with the package installed (`pip install -e .[dev]`);
    Pillow (a dependency of the label printing) draws the print previews and packs the pictures,
    `websockets` (a dependency of the live channel) talks to the browser. Nothing else is needed.
  * a Chromium browser: Edge or Chrome are looked for in the usual places on Windows, macOS and
    Linux; the environment variable `BROWSER` names another one (the path of the program).
  * no network: the pictures are taken from 127.0.0.1; Bambuddy is the in-memory stand-in, and the
    one HTTP question the set-up pages ask Bambuddy (`/api/v1/auth/me`) is answered by a mock transport.

How the data comes about
  The set-up pages are filled in and sent in the browser. The rest runs through the code of the
  application: articles, parts, sets, orders and bookings through the functions in
  `reprintbuddy.domain`; prints through the real tick of the engine, driven by hand
  (`ticker.tick(poll=True)`) while the script changes the state of the Bambuddy stand-in
  (`set_status`). The clock of the application is fixed (`Clock`), so a second run draws the same
  dates. A few settings are written straight into the database through `settings.set_setting`
  (automation on, queue cap, the Home Assistant address); that is marked in `Scenario`.

Each picture is a quantised PNG of at most 150 KB (the script stops when one is bigger).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import dataclasses
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from websockets.sync.client import connect as ws_connect

ROOT = Path(__file__).resolve().parents[1]

from reprintbuddy import models  # noqa: E402
from reprintbuddy import settings as st  # noqa: E402
from reprintbuddy.activity import capture as activity_capture  # noqa: E402
from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS  # noqa: E402
from reprintbuddy.bambuddy.fake import FakeBambuddy  # noqa: E402
from reprintbuddy.bambuddy.types import FilamentRequirement  # noqa: E402
from reprintbuddy.domain import assembled, catalog, filament, orders, pauses, reorder, seen, stock, workqueue  # noqa: E402
from reprintbuddy.ops.context import OpsContext  # noqa: E402
from reprintbuddy.ops.runner import CommandResult  # noqa: E402
from reprintbuddy.web import app as web_app  # noqa: E402
from reprintbuddy.web.app import create_app  # noqa: E402
from reprintbuddy.web.routes import ops as ops_routes  # noqa: E402

WIDTH = 1280
MOBILE_WIDTH = 375
MAX_HEIGHT = 1100
HARD_LIMIT = 150 * 1024  # bytes per picture (tests/test_docs.py)
SOFT_LIMIT = 48 * 1024  # what the packer aims at, so both folders stay far under 3 MB
BAMBUDDY_ADDRESS = "http://bambuddy.example:8000"
HOME_ASSISTANT_ADDRESS = "http://homeassistant.example:8123"
QR_ADDRESS = "http://reprintbuddy.example:8080"
LABEL_PRINTER = "192.0.2.50"
KEY_TEXT = "bb_example_key"  # typed into a password field: it shows as dots

# The application's own list, not a copy: a copy missed `settings:read` when 0.4.0 made it required
# (p342) and the connection check of the pictures failed (tests/test_docs_screenshots_rights.py).
REQUIRED = REQUIRED_PERMISSIONS

ALL_NAMES = (
    "setup-admin", "setup-group", "setup-key", "setup-connect", "overview", "articles-list",
    "article-new-files", "article-new-plates", "article-new-form", "article-upload",
    "article-filaments", "article-detail", "queue", "inspection", "assembly", "label-preview",
    "reorder", "filament-page", "filament-settings", "orders", "parts", "sets", "set-form", "bookings", "journal",
    "assembled-form", "assembled-detail",
    "settings-bambuddy", "settings-automation", "settings-notifications", "settings-labels",
    "settings-log", "settings-update", "articles-mobile", "overview-mobile",
)


# ---------------------------------------------------------------------------------------------
# the clock of the application


class Clock:
    """A clock that stands still until the script moves it (naive UTC, like the application's)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


CLOCK = Clock(datetime(2026, 11, 2, 8, 0))

LANGUAGE = {"now": "en"}


def tr(english: str, german: str) -> str:
    """Texts that people type (notes, shelf labels, order references) follow the language of the pictures."""
    return german if LANGUAGE["now"] == "de" else english


def freeze_application_clock() -> None:
    """Every place that asks `models.utcnow` (imported by name into many modules) gets the fixed clock.

    The column defaults (`created_at` and the like) hold the original function; they are replaced too.
    """
    original = models.utcnow
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("reprintbuddy"):
            for name, value in list(vars(module).items()):
                if value is original:
                    setattr(module, name, CLOCK)
    from sqlalchemy import ColumnDefault

    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            default = column.default
            if default is not None and getattr(default, "is_callable", False):
                try:
                    probe = default.arg(None)
                except Exception:
                    continue
                if isinstance(probe, datetime):
                    column.default = ColumnDefault(lambda ctx: CLOCK())


# ---------------------------------------------------------------------------------------------
# neutral pictures for the print files


def _thumbnail(kind: str, plate: int, size: int = 240) -> bytes:
    """A small made-up preview: a coloured shape on a dark plate (no real model is drawn)."""
    palettes = {
        "bracket": ((222, 226, 232), (170, 176, 188)),
        "bracket-large": ((222, 226, 232), (170, 176, 188)),
        "clip": ((60, 64, 72), (110, 116, 128)),
        "clip-blue": ((46, 108, 190), (28, 74, 140)),
        "guide": ((52, 56, 64), (226, 62, 58)),
        "holder": ((70, 74, 84), (120, 126, 138)),
        "label": ((236, 238, 242), (200, 60, 56)),
        "hook": ((210, 214, 220), (150, 156, 168)),
    }
    top, side = palettes[kind]
    image = Image.new("RGB", (size, size), (38, 40, 44))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, size - 14, size - 14), 16, outline=(70, 74, 82), width=2)
    c = size // 2
    if kind == "bracket":
        draw.polygon([(60, 60), (110, 60), (110, 130), (180, 130), (180, 180), (60, 180)], fill=side)
        draw.polygon([(60, 52), (110, 52), (110, 122), (180, 122), (180, 172), (60, 172)], fill=top)
        draw.ellipse((74, 140, 94, 160), fill=(38, 40, 44))
    elif kind == "bracket-large":
        draw.polygon([(44, 44), (116, 44), (116, 110), (200, 110), (200, 196), (44, 196)], fill=side)
        draw.polygon([(44, 36), (116, 36), (116, 102), (200, 102), (200, 188), (44, 188)], fill=top)
        for x, y in ((66, 150), (100, 150), (150, 150)):
            draw.ellipse((x, y, x + 20, y + 20), fill=(38, 40, 44))
    elif kind in ("clip", "clip-blue"):
        offsets = ((70, 80), (150, 80), (70, 150), (150, 150)) if plate == 1 else ((110, 78), (110, 150))
        for x, y in offsets:
            draw.rounded_rectangle((x - 2, y + 8, x + 52, y + 54), 12, fill=side)
            draw.rounded_rectangle((x, y, x + 50, y + 46), 12, fill=top)
            draw.ellipse((x + 15, y + 12, x + 35, y + 32), fill=(38, 40, 44))
    elif kind == "guide":
        draw.rounded_rectangle((38, 96, 202, 150), 22, fill=side)
        draw.rounded_rectangle((38, 88, 202, 142), 22, fill=top)
        draw.rounded_rectangle((70, 104, 170, 126), 10, fill=(38, 40, 44))
    elif kind == "holder":
        if plate == 1:
            draw.ellipse((46, 52, 196, 202), fill=side)
            draw.ellipse((46, 44, 196, 194), fill=top)
            draw.ellipse((96, 94, 146, 144), fill=(38, 40, 44))
        else:
            draw.rounded_rectangle((60, 60, 180, 190), 14, fill=side)
            draw.rounded_rectangle((60, 52, 180, 182), 14, fill=top)
    elif kind == "label":
        draw.rounded_rectangle((50, 70, 190, 170), 10, fill=side)
        draw.rounded_rectangle((50, 62, 190, 162), 10, fill=top)
        draw.rectangle((70, 90, 170, 100), fill=(60, 64, 72))
        draw.rectangle((70, 112, 140, 120), fill=(60, 64, 72))
    else:
        draw.arc((56, 40, 184, 168), 200, 340, fill=top, width=22)
        draw.rounded_rectangle((100, 40, 140, 190), 12, fill=top)
        draw.ellipse((108, 160, 132, 184), fill=(38, 40, 44))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class ShotBambuddy(FakeBambuddy):
    """The stand-in with real pictures for the thumbnails and the application's clock."""

    def __init__(self) -> None:
        super().__init__()
        self.kinds: dict[int, str] = {}
        self.now = CLOCK  # Bambuddy's clock: `created_at` of its entries

    def add_file(self, filename, folder_id=None, *, kind="clip", **options):  # type: ignore[override]
        file = super().add_file(filename, folder_id, **options)
        self.kinds[file.id] = kind
        return file

    async def plate_thumbnail(self, file_id: int, plate_index: int) -> bytes:
        await super().plate_thumbnail(file_id, plate_index)  # the checks (unknown file or plate)
        return _thumbnail(self.kinds[file_id], plate_index)

    async def file_thumbnail(self, file_id: int):
        await super().file_thumbnail(file_id)
        return "image/png", _thumbnail(self.kinds[file_id], 1)

    def set_status(self, item_id: int, status: str, *, printer_id: int | None = None) -> None:
        super().set_status(item_id, status, printer_id=printer_id)
        item = self._items[item_id]
        changes: dict[str, object] = {}
        if status == "printing":
            changes["started_at"] = CLOCK()
        elif status in ("completed", "failed", "skipped", "cancelled"):
            changes["completed_at"] = CLOCK()
        self._put(dataclasses.replace(item, **changes))


# ---------------------------------------------------------------------------------------------
# the app, served on a local port


class Mock:
    """What the one HTTP question to Bambuddy (`/api/v1/auth/me`) is answered with."""

    def __init__(self) -> None:
        self.permissions: list[str] = list(REQUIRED)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json={
                "id": 7, "username": "reprintbuddy", "is_admin": False,
                "groups": [{"id": 2, "name": "Reprintbuddy"}], "permissions": list(self.permissions),
            })
        return httpx.Response(503)

    def factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# The pictures show this version, never the installed one (p190): the menu foot, the line "installed" and
# the card "Update" would otherwise be out of date with every release and every picture would have to be
# taken again for a step of the version alone. The stand-in starts with 1 on purpose: no release of the
# project looks like it, so nobody takes it for an old version.
SHOT_VERSION = "1.2.3"


def next_version() -> str:
    """The version after the stand-in: the card "Update" shows it as the newer release."""
    major, minor, patch = (int(part) for part in SHOT_VERSION.split("."))
    return f"{major}.{minor}.{patch + 1}"


def pin_version() -> None:
    """Makes the application tell the stand-in as its version (menu foot, `?v=` of the files, the live
    channel, the line of the card "Update", the log entry "started"). The modules read the name when they
    are used, so this holds for the application created afterwards."""
    web_app.__version__ = SHOT_VERSION
    ops_routes.__version__ = SHOT_VERSION


def release_runner(args, *, cwd=None, timeout=120.0) -> CommandResult:
    """Stands in for git and systemd on the card "Update": a newer release (the next patch version) exists
    on a repository that is invented (`example/reprintbuddy`)."""
    joined = " ".join(str(a) for a in args)
    if "describe" in joined:
        return CommandResult(0, f"v{SHOT_VERSION}\n")
    if "ls-remote" in joined:
        tags = [("0123abc", "0.3.11"), ("0123abd", SHOT_VERSION), ("0123abe", next_version())]
        return CommandResult(0, "".join(f"{sha}\trefs/tags/v{tag}\n" for sha, tag in tags))
    if "get-url" in joined:
        return CommandResult(0, "https://github.com/example/reprintbuddy.git\n")
    if "ActiveState" in joined:
        return CommandResult(0, "inactive\n")
    return CommandResult(0)


def release_notes_answer(url, headers) -> tuple[int, bytes]:
    """What GitHub's API would answer for the invented repository: one invented release, no network."""
    lines = [
        "## Changes",
        "",
        "- Messages at the top of a page go away by themselves.",
        "- The stock numbers say from when they are.",
        "- A stored token never goes to another address.",
    ]
    releases = [
        {
            "tag_name": f"v{next_version()}", "published_at": "2026-11-08T09:00:00Z", "draft": False, "prerelease": False,
            "body": "\n".join(lines),
        },
    ]
    return 200, json.dumps(releases).encode()


class Server:
    """The application behind uvicorn on 127.0.0.1, in a thread of this process."""

    def __init__(self, workdir: Path, language: str) -> None:
        self.mock = Mock()
        self.fake = ShotBambuddy()
        pin_version()
        install = workdir / "install"
        (install / ".git").mkdir(parents=True)
        self.app = create_app(
            workdir / "reprintbuddy.db",
            http_client_factory=self.mock.factory,
            ops=OpsContext(install_dir=install, runner=release_runner, http_get=release_notes_answer),
            run_engine=False,
        )
        self.app.state.bambuddy_factory = lambda: self.fake
        self.app.state.ticker.now = CLOCK
        # What the scenario writes is written "by the user", as if it had been done in the interface: the
        # log page shows the person, not "System" (what a session without an actor counts as).
        real = self.app.state.sessionmaker

        class UserSession(real.class_):  # type: ignore[name-defined, misc]
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.info[activity_capture.ACTOR] = "user"  # its own dict: the capture keeps its state in it

        self.sm = sessionmaker(**{**real.kw, "class_": UserSession})
        with self.sm() as session:
            st.set_setting(session, "language", language)  # the language of the set-up pages too
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="on",
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("the application did not start")

    def stop(self) -> None:
        self.app.state.live.close()  # ends the open event streams, so the shutdown does not wait for them
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(10)
        self.app.state.engine.dispose()

    def tick(self, poll: bool = True):
        return asyncio.run(self.app.state.ticker.tick(poll=poll))


# ---------------------------------------------------------------------------------------------
# the browser


def find_browser() -> str:
    env = os.environ.get("BROWSER")
    if env:
        if Path(env).exists() or shutil.which(env):
            return env
        raise SystemExit(f"BROWSER={env} was not found")
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if Path(path).exists():
            return path
    raise SystemExit("no Chromium browser found: install Edge or Chrome, or set BROWSER")


NO_MOTION = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "caret-color:transparent!important;scroll-behavior:auto!important}"
    "::selection{background:transparent!important}"
)


class Browser:
    """A headless browser steered over the DevTools protocol (a WebSocket, JSON messages)."""

    def __init__(self, workdir: Path, lang: str = "en") -> None:
        self.profile = workdir / "profile"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.process = subprocess.Popen(
            [
                find_browser(), "--headless=new", f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking", "--disable-sync",
                "--hide-scrollbars", "--force-device-scale-factor=1", "--mute-audio",
                "--disable-features=Translate,MediaRouter", f"--lang={lang}", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        http = httpx.Client(trust_env=False, timeout=5)
        ws_url = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ws_url is None:
            try:
                pages = http.get(f"http://127.0.0.1:{port}/json/list").json()
                ws_url = next((p["webSocketDebuggerUrl"] for p in pages if p.get("type") == "page"), None)
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
        http.close()
        if ws_url is None:
            self.close()
            raise SystemExit("the browser did not answer on the DevTools port")
        self._stack = contextlib.ExitStack()  # `connect` is a context manager (also in older versions)
        self.ws = self._stack.enter_context(ws_connect(ws_url, max_size=None, open_timeout=15))
        self._id = 0
        self.events: list[dict] = []
        self.call("Page.enable")
        self.call("Network.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", source=(
            "document.addEventListener('DOMContentLoaded',()=>{const s=document.createElement('style');"
            f"s.textContent={json.dumps(NO_MOTION)};document.head.appendChild(s);}});"
        ))
        self.call("Emulation.setLocaleOverride", locale="de-DE" if lang == "de" else "en-US")
        self.call("Network.setCacheDisabled", cacheDisabled=True)

    def call(self, method: str, **params):
        self._id += 1
        mine = self._id
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=90))
            if message.get("id") == mine:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)

    def wait_event(self, name: str, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            for i, event in enumerate(self.events):
                if event["method"] == name:
                    del self.events[i]
                    return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                message = json.loads(self.ws.recv(timeout=left))
            except TimeoutError:
                return False
            if "method" in message:
                self.events.append(message)

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise RuntimeError(f"script failed: {details.get('exception', {}).get('description') or details.get('text')} in {expression[:80]}")
        return result["result"].get("value")

    def viewport(self, width: int, height: int, mobile: bool = False) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1,
            mobile=mobile, screenWidth=width, screenHeight=height,
        )

    def goto(self, url: str, settle: float = 0.9) -> None:
        self.events.clear()
        self.call("Page.navigate", url=url)
        if not self.wait_event("Page.loadEventFired", 40):
            raise RuntimeError(f"{url} did not load")
        self.settle(settle)

    def settle(self, pause: float = 0.9) -> None:
        """Quiet page: pictures loaded, fonts ready, the live channel connected, then a short pause."""
        self.evaluate(
            "Promise.race([Promise.all([document.fonts.ready,"
            "...[...document.images].map(i=>i.complete?1:new Promise(r=>{i.onload=i.onerror=r}))]),"
            "new Promise(r=>setTimeout(r,8000))])"
        )
        time.sleep(pause)

    def wait_for(self, expression: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.15)
        raise RuntimeError(f"timeout waiting for {expression}")

    def submit(self, selector: str) -> None:
        """Click a button (or any element) and wait for the page that follows."""
        self.events.clear()
        self.evaluate(f"document.querySelector({json.dumps(selector)}).click()")
        self.wait_event("Page.loadEventFired", 40)
        self.settle()

    def screenshot(self, clip: dict | None = None) -> Image.Image:
        params: dict = {"format": "png", "captureBeyondViewport": clip is not None}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.call("Page.captureScreenshot", **params)["data"]
        return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("Browser.close")  # the browser ends all its processes itself (terminate leaves helpers behind)
        with contextlib.suppress(Exception):
            self._stack.close()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ---------------------------------------------------------------------------------------------
# packing the pictures


def pack(image: Image.Image, path: Path) -> int:
    """Palette PNG: as many colours as fit the soft limit (not fewer than 64 unless the hard limit forces it)."""
    last = b""
    for colors in (256, 192, 128, 96, 64, 48, 32):
        quantised = image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        out = io.BytesIO()
        quantised.save(out, "PNG", optimize=True)
        last = out.getvalue()
        if len(last) <= SOFT_LIMIT or (len(last) <= HARD_LIMIT and colors <= 64):
            break
    if len(last) > HARD_LIMIT:
        raise SystemExit(f"{path.name}: {len(last)} bytes, more than the limit of {HARD_LIMIT}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(last)
    return len(last)


# ---------------------------------------------------------------------------------------------
# the scenario


@dataclass
class Scenario:
    """Names of what was made: ids of articles, files and the like."""

    articles: dict[str, int] = field(default_factory=dict)
    files: dict[str, int] = field(default_factory=dict)
    folder: int = 0
    sets: dict[str, int] = field(default_factory=dict)
    parts: dict[str, int] = field(default_factory=dict)
    sorts: dict[str, int] = field(default_factory=dict)  # spool sorts whose settings are shown on their own


def at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    """A moment in November 2026 (naive UTC)."""
    return datetime(2026, 11, day, hour, minute)


def fill_bambuddy(fake: ShotBambuddy, scn: Scenario) -> None:
    """The print files, printers and spools of the made-up Bambuddy."""
    folder = fake.add_folder("Examples")
    scn.folder = folder.id

    def file(name, kind, model="A1", plates=1, reqs=None, minutes=60):
        # the slicer's print time, which Bambuddy puts on every queue entry (the print plan, 0.4.2)
        made = fake.add_file(name, folder.id, kind=kind, printer_model=model, plates=plates, print_time_s=minutes * 60)
        for plate, req in (reqs or {}).items():
            fake.set_requirements(made.id, plate, [FilamentRequirement(*r) for r in req])
        scn.files[name.removesuffix(".gcode.3mf")] = made.id

    white, black, red, blue = "#FFFFFF", "#000000", "#D32F2F", "#1E5AA8"
    file("Bracket small.gcode.3mf", "bracket", reqs={1: [(1, 18.4, "PLA", white)]}, minutes=50)
    file("Bracket large.gcode.3mf", "bracket-large", reqs={1: [(1, 42.0, "PLA", white)]}, minutes=110)
    file("Clip set.gcode.3mf", "clip", plates=2, reqs={1: [(1, 36.5, "PETG", black)], 2: [(1, 36.5, "PETG", blue)]})
    file("Cable guide.gcode.3mf", "guide", model="P1S", reqs={1: [(1, 11.2, "PLA", black), (2, 1.8, "PLA", red)]})
    file("Spool holder.gcode.3mf", "holder", plates=2, reqs={1: [(1, 84.0, "PETG", black)], 2: [(1, 52.0, "PETG", black)]})
    file("Label holder.gcode.3mf", "label", reqs={1: [(1, 6.2, "PLA", white), (2, 1.1, "PLA", red)]})
    file("Hook.gcode.3mf", "hook", reqs={1: [(1, 14.0, "PLA", black)]})
    # parts of articles made of parts (0.4.0, p341): ten panels of a box, the parts of a hose
    file("Box panels.gcode.3mf", "label", plates=10, reqs={n: [(1, 9.5, "PLA", white)] for n in range(1, 11)})
    file("Hose parts.gcode.3mf", "hook", plates=7, reqs={n: [(1, 7.0, "PLA", black)] for n in range(1, 8)})
    fake.add_printer("Printer 1", "A1")
    fake.add_printer("Printer 2", "A1")
    fake.add_printer("Printer 3", "P1S")

    def spool(material, color_name, color_hex, remaining, subtype=None):
        color_name = {"White": tr("White", "Weiß"), "Black": tr("Black", "Schwarz"), "Red": tr("Red", "Rot"),
                      "Blue": tr("Blue", "Blau")}[color_name]
        fake.add_spool(
            material, brand="Example Brand", subtype=subtype, color_name=color_name, color_hex=color_hex,
            label_g=1000, remaining_g=remaining, created_at=at(1),
        )

    spool("PLA", "White", white, 820)
    spool("PLA", "White", white, 1000)
    spool("PLA", "White", white, 410)
    spool("PLA", "White", white, 500, subtype="Matte")
    spool("PLA", "Black", black, 640)
    spool("PLA", "Black", black, 1000)
    spool("PLA", "Red", red, 700)
    spool("PETG", "Black", black, 260)
    spool("PETG", "Black", black, 120)
    spool("PETG", "Blue", blue, 900)


def build_catalog(server: Server, scn: Scenario) -> None:
    """Parts, articles, sets, filament settings: through the functions of `reprintbuddy.domain`."""
    sm, fake = server.sm, server.fake
    CLOCK.set(at(2, 8))
    with sm.begin() as s:
        for name, stock_n, minimum, pack_size, lead, url in (
            ("Insert M4", 40, 120, 100, 7, "https://supplier.example/insert-m4"),
            ("Screw M4×8", 480, 200, 500, 5, "https://supplier.example/screw-m4x8"),
            ("Rubber foot", 90, 50, 100, 10, "https://supplier.example/rubber-foot"),
        ):
            suppliers = [catalog.SupplierInput(
                name="Example Supplier", order_url=url, pack_size=pack_size, min_order_packs=1,
                lead_time_days=lead, is_default=True,
            )]
            if name == "Insert M4":  # a second supplier with other packs (Spec 5.3), shown on the reorder page
                suppliers.append(catalog.SupplierInput(
                    name="Example Supplier B", order_url="https://supplier-b.example/insert-m4", pack_size=25,
                    min_order_packs=2, lead_time_days=2,
                ))
            part = catalog.create_part(
                s,
                catalog.PartInput(
                    name=name, unit="pcs", min_stock=minimum, safety_value=5, safety_unit="days",
                    suppliers=suppliers,
                ),
                stock_n, "user",
            )
            scn.parts[name] = part.id

    def article(key, file_name, plate, *, name=None, sku, label_text, thumbnail_plate=None, **data):
        file_id = scn.files[file_name]
        file = asyncio.run(fake.get_file(file_id))
        plates = asyncio.run(fake.list_plates(file_id))
        reqs = asyncio.run(fake.filament_requirements(file_id, plate))
        thumb = asyncio.run(fake.plate_thumbnail(file_id, plate))
        with sm.begin() as s:
            made = catalog.create_article(
                s, file=file, plate=plates[plate - 1], requirements=reqs, thumbnail=thumb,
                data=catalog.ArticleInput(
                    name=name or file_name, sku=sku, label_text=label_text, target_model=file.printer_model,
                    **data,
                ),
            )
            scn.articles[key] = made.id

    article("bracket_small", "Bracket small", 1, sku="EX-001", label_text=tr("Shelf A1", "Regal A1"),
            units_per_print=4, min_stock=12, buffer=4, auto_enabled=True)
    article("bracket_large", "Bracket large", 1, sku="EX-002", label_text=tr("Shelf A2", "Regal A2"),
            units_per_print=2, min_stock=8, buffer=2, auto_enabled=True)
    article("clip_set", "Clip set", 1, sku="EX-003", label_text=tr("Shelf B1", "Regal B1"),
            units_per_print=8, min_stock=16, buffer=8, auto_enabled=False)
    article("clip_blue", "Clip set", 2, name="Clip set blue", sku="EX-004", label_text=tr("Shelf B2", "Regal B2"),
            units_per_print=8, min_stock=8, buffer=0, auto_enabled=True)
    article("cable_guide", "Cable guide", 1, sku="EX-005", label_text=tr("Shelf C1", "Regal C1"),
            units_per_print=6, min_stock=16, buffer=6, auto_enabled=True, needs_assembly=True,
            parts={scn.parts["Insert M4"]: 2, scn.parts["Screw M4×8"]: 2})
    article("spool_holder", "Spool holder", 1, sku="EX-006", label_text=tr("Shelf C2", "Regal C2"),
            units_per_print=1, min_stock=2, buffer=0, auto_enabled=True)

    # Articles made of parts (Spec 5.6, 0.4.0 p341): a storage box of ten panels built on stock, and a
    # hose of twelve segments with four options of two parts each, built on order.
    for n, letter in enumerate("ABCDEFGHIJ", start=1):
        article(f"panel_{letter}", "Box panels", n, name=tr(f"Panel {letter}", f"Platte {letter}"), sku=None,
                label_text=None, units_per_print=1)
    for n, (key, en_name, de_name, alone) in enumerate((
        ("segment", "Segment", "Segment", True),
        ("side_l", "Side part L", "Seitenteil L", False), ("side_r", "Side part R", "Seitenteil R", False),
        ("socket_l32", "Socket L32", "Anschluss L32", False), ("socket_l40", "Socket L40", "Anschluss L40", False),
        ("socket_r32", "Socket R32", "Anschluss R32", False), ("socket_r40", "Socket R40", "Anschluss R40", False),
    ), start=1):
        article(key, "Hose parts", n, name=tr(en_name, de_name), sku=None, label_text=None, units_per_print=1,
                sold_alone=alone)
    with sm.begin() as s:
        a = scn.articles
        B = catalog.BaseInput
        box = assembled.create_assembled(s, assembled.AssembledInput(
            name=tr("Storage box", "Box"), sku="EX-B1", mode="stock",
            base=[B(1, a[f"panel_{c}"]) for c in "ABCDEFGHIJ"], fields=[],
            parts={scn.parts["Screw M4×8"]: 8}, label_text=tr("Shelf D1", "Regal D1"), min_stock=2, buffer=1,
        ))
        scn.articles["box"] = box.id

        def option(suffix, side, socket):
            return assembled.OptionInput(None, suffix, [B(1, a[side]), B(1, a[socket])])

        hose = assembled.create_assembled(s, assembled.AssembledInput(
            name=tr("Hose", "Schlauch"), sku="EX-H1", mode="order", base=[B(12, a["segment"])],
            fields=[[option("-L32", "side_l", "socket_l32"), option("-L40", "side_l", "socket_l40"),
                     option("-R32", "side_r", "socket_r32"), option("-R40", "side_r", "socket_r40")]],
            parts={scn.parts["Rubber foot"]: 2},
        ))
        scn.articles["hose"] = hose.id

    with sm.begin() as s:
        a = scn.articles
        # Sets with base parts and choice parts (Spec 5.4, p340): the kit has a variant per bracket,
        # the assortment two fields of choice parts, so the list shows both kinds of variants.
        scn.sets["Bracket kit"] = catalog.create_set(s, catalog.SetInput(
            "Bracket kit", "EX-S01", [catalog.BaseInput(2, a["clip_set"])],
            [catalog.ChoiceInput(1, [catalog.ChoiceOptionInput(a["bracket_small"], "-S"),
                                     catalog.ChoiceOptionInput(a["bracket_large"], "-L")])],
        )).id
        scn.sets["Clip assortment"] = catalog.create_set(s, catalog.SetInput(
            "Clip assortment", "EX-S02", [catalog.BaseInput(1, a["cable_guide"])],
            [catalog.ChoiceInput(2, [catalog.ChoiceOptionInput(a["clip_blue"], "-B")]),
             catalog.ChoiceInput(1, [catalog.ChoiceOptionInput(a["spool_holder"], "-1")])],
        )).id
        # A set of two sets (Spec 5.4, 0.4.3 p423) that bring their choice parts (0.4.4 p441): the list
        # shows the inner sets with their numbers and the badge "Set" and the variants EX-S03-S-B-1 and
        # EX-S03-L-B-1; the open order 1003 shows the inner sets indented with their variant numbers.
        scn.sets["Workshop bundle"] = catalog.create_set(s, catalog.SetInput(
            "Workshop bundle", "EX-S03",
            [catalog.BaseInput(1, None, scn.sets["Bracket kit"]),
             catalog.BaseInput(1, None, scn.sets["Clip assortment"])],
            [],
        )).id


    # the spool sorts of the stand-in are entered the way the filament page does it
    spools = asyncio.run(fake.list_spools())
    with sm.begin() as s:
        filament.sync_sorts(s, spools)
        sorts = {
            (row.material, row.subtype, row.color_name): row.id
            for row in s.scalars(select(models.FilamentSort))
        }
        white, black, red, blue = tr("White", "Weiß"), tr("Black", "Schwarz"), tr("Red", "Rot"), tr("Blue", "Blau")
        pla_white, pla_matte = sorts[("PLA", None, white)], sorts[("PLA", "Matte", white)]
        pla_black, pla_red = sorts[("PLA", None, black)], sorts[("PLA", None, red)]
        petg_black, petg_blue = sorts[("PETG", None, black)], sorts[("PETG", None, blue)]
        a = scn.articles
        filament.set_groups(s, a["bracket_small"], {1: ([pla_white, pla_matte], pla_white)})
        filament.set_groups(s, a["bracket_large"], {1: ([pla_white], None)})
        filament.set_groups(s, a["clip_set"], {1: ([petg_black], None)})
        filament.set_groups(s, a["clip_blue"], {1: ([petg_blue], None)})
        filament.set_groups(s, a["cable_guide"], {1: ([pla_black], None), 2: ([pla_red], None)})
        filament.set_groups(s, a["spool_holder"], {1: ([petg_black], None)})
        scn.sorts["petg_black"] = petg_black
        for sort_id, lead, pack_g, min_packs in (
            (pla_white, 7, 1000, 2), (petg_black, 10, 1000, 1), (pla_black, 7, 1000, 1),
            (pla_red, 7, 1000, 1), (petg_blue, 10, 1000, 1),
        ):
            suppliers = [catalog.SupplierInput(
                name="Example Supplier", order_url="https://supplier.example/filament", pack_size=pack_g,
                min_order_packs=min_packs, lead_time_days=lead, is_default=True,
            )]
            if sort_id == petg_black:  # a second supplier with smaller rolls (Spec 9.3), shown on the reorder page
                suppliers.append(catalog.SupplierInput(
                    name="Example Supplier B", order_url="https://supplier-b.example/filament", pack_size=500,
                    min_order_packs=1, lead_time_days=3,
                ))
            filament.save_settings(s, sort_id, filament.FilamentSettingsInput(
                safety_value=7, safety_unit="days", suppliers=suppliers,
            ))


def build_history(server: Server, scn: Scenario) -> None:
    """Bookings of the last days (the journal), usage records, orders, an "ordered" mark."""
    sm, fake = server.sm, server.fake
    a = scn.articles

    # the daily record of the spool stock: a little less each day (the source of the usage)
    spools = asyncio.run(fake.list_spools())
    rate = {"PETG": 38.0, "PLA": 24.0}
    for back in range(12, 0, -1):
        shown = [dataclasses.replace(sp, remaining_g=sp.remaining_g + rate[sp.material] * back) for sp in spools]
        with sm.begin() as s:
            filament.record_snapshot(s, shown, date(2026, 11, 9) - timedelta(days=back))

    CLOCK.set(at(2, 9, 30))
    with sm.begin() as s:
        for key, counted, note in (
            ("bracket_small", 22, tr("Counted shelf A1", "Regal A1 gezählt")), ("bracket_large", 5, tr("Counted shelf A2", "Regal A2 gezählt")),
            ("clip_set", 12, tr("Counted shelf B1", "Regal B1 gezählt")), ("clip_blue", 14, tr("Counted shelf B2", "Regal B2 gezählt")),
            ("cable_guide", 9, tr("Counted shelf C1", "Regal C1 gezählt")), ("spool_holder", 2, tr("Counted shelf C2", "Regal C2 gezählt")),
        ):
            stock.count_article(s, a[key], "stock", counted, "user", note)
        # the hose example of the documentation (Spec 5.6): together at most 4
        for key, counted in (("segment", 60), ("side_l", 3), ("side_r", 1), ("socket_l32", 2), ("socket_l40", 2),
                             ("socket_r40", 1), *((f"panel_{c}", 1 if c in "AB" else 2) for c in "ABCDEFGHIJ")):
            stock.count_article(s, a[key], "stock", counted, "user", tr("Counted shelf D", "Regal D gezählt"))
    CLOCK.set(at(3, 14, 10))
    with sm.begin() as s:
        stock.receive_part(s, scn.parts["Insert M4"], 50, "user", tr("Delivery 2026-11-03", "Lieferung 03.11.2026"))
    CLOCK.set(at(4, 10, 20))
    with sm.begin() as s:
        order = orders.create_order(
            s, reference=tr("Order 1000", "Bestellung 1000"), order_date=date(2026, 11, 4), actor="user", note=tr("Pickup on Friday", "Abholung am Freitag"),
            items=[orders.ArticleItem(a["bracket_small"], 4), orders.ArticleItem(a["clip_blue"], 2)],
        )
        shipped = order.id
    CLOCK.set(at(5, 15, 5))
    with sm.begin() as s:
        orders.ship_order(s, shipped, "user", tr("Handed over", "Übergeben"))
    CLOCK.set(at(5, 16, 40))
    with sm.begin() as s:
        stock.stock_in(s, a["bracket_small"], 8, "user", tr("Printed in the evening", "Abends gedruckt"), consume_parts=False)
    CLOCK.set(at(6, 11, 15))
    with sm.begin() as s:
        stock.sell_unreserved(s, a["clip_set"], 5, "user", tr("Sold at the market", "Auf dem Markt verkauft"))
        reorder.mark_ordered(s, "part", scn.parts["Screw M4×8"], 1)
    CLOCK.set(at(7, 9, 45))
    with sm.begin() as s:
        orders.create_order(
            s, reference=tr("Order 1001", "Bestellung 1001"), order_date=date(2026, 11, 7), actor="user", note=tr("Ship with the next parcel", "Mit dem nächsten Paket versenden"),
            items=[orders.ArticleItem(a["bracket_small"], 3), orders.ArticleItem(a["clip_blue"], 2),
                   orders.SetItem(scn.sets["Bracket kit"], 1, (a["bracket_small"],))],
        )
    CLOCK.set(at(8, 13, 20))
    with sm.begin() as s:
        orders.create_order(
            s, reference=tr("Order 1002", "Bestellung 1002"), order_date=date(2026, 11, 8), actor="user",
            items=[orders.ArticleItem(a["cable_guide"], 2), orders.ArticleItem(a["bracket_large"], 1),
                   orders.AssembledItem(a["hose"], 1, _hose_key(s, a["hose"], "-L32"))],
        )
        orders.create_order(
            s, reference=tr("Order 1003", "Bestellung 1003"), order_date=date(2026, 11, 8), actor="user",
            items=[orders.SetItem(scn.sets["Workshop bundle"], 1, (a["bracket_small"], a["clip_blue"], a["spool_holder"]))],
        )
        stock.count_article(s, a["cable_guide"], "awaiting_assembly", 6, "user", tr("Printed parts waiting for inserts", "Gedruckte Teile warten auf Einsätze"))
        pauses.pause_article(s, a["spool_holder"], "two_failures", "auto")
        pauses.pause_article(s, a["clip_blue"], "cancelled_in_bambuddy", "auto")


def _hose_key(s, hose_id: int, suffix: str) -> str:
    """The variant key of the option with `suffix` (the order form sends the same)."""
    composite = s.get(models.Article, hose_id)
    return next(str(o.id) for o in assembled.options(s, composite)[0] if o.suffix == suffix)


def run_prints(server: Server, scn: Scenario) -> None:
    """Today: the automation fills the work queue and hands prints to the stand-in; some are
    printing, some finished (they wait to be inspected). All through the real tick."""
    sm, fake = server.sm, server.fake
    CLOCK.set(at(9, 8, 30))
    with sm() as s:  # settings written through the settings module (there is no form for these two on the way)
        st.set_setting(s, "automation", True)
        st.set_setting(s, "queue_cap", 5)
    server.tick(poll=True)

    by_file = {v: k for k, v in scn.files.items()}
    items = sorted(asyncio.run(fake.list_all_items()), key=lambda i: i.id)
    named = [(i, by_file.get(i.library_file_id)) for i in items]
    done: set[int] = set()

    def first(name: str):
        return next((i for i, n in named if n == name and i.id not in done), None)

    CLOCK.set(at(9, 9, 10))
    for name, printer in (("Bracket large", 1), ("Cable guide", 3)):
        item = first(name)
        if item is not None:
            fake.set_status(item.id, "completed", printer_id=printer)
            done.add(item.id)
    printing = first("Bracket large")
    if printing is not None:
        fake.set_status(printing.id, "printing", printer_id=1)
        done.add(printing.id)
    # The print plan (0.4.2): Printer 1 prints the large bracket, 63 % done, 70 minutes left; Printer 2 has an
    # entry Bambuddy assigned to it (somebody else's); the rest of the queue is expected on the free printers.
    fake.set_printer_status(1, state="RUNNING", progress=63.0, remaining_min=70, subtask_name="Bracket large")
    fake.add_foreign_item(tr("Spare knob", "Ersatzknauf"), status="pending", created_by="admin", printer_id=2,
                          print_time_s=35 * 60)
    CLOCK.set(at(9, 10, 5))
    server.tick(poll=True)

    # somebody else's entry in Bambuddy; wanted prints that wait for a free place in Reprintbuddy's own queue
    fake.add_foreign_item(tr("Calibration cube", "Kalibrierwürfel"), status="pending", created_by="admin", library_file_id=scn.files["Hook"], plate_id=1,
                          target_model="A1", print_time_s=25 * 60)
    CLOCK.set(at(9, 10, 30))
    server.tick(poll=True)
    with sm.begin() as s:
        workqueue.add_entry(s, scn.articles["bracket_small"], 2, "manual", "user")
        held = workqueue.add_entry(s, scn.articles["clip_set"], 1, "manual", "user")
        workqueue.hold_entry(s, held.id, "user")


# ---------------------------------------------------------------------------------------------
# taking the pictures


class Shooter:
    """Takes and saves the pictures of one language."""

    def __init__(self, browser: Browser, server: Server, out: Path, lang: str, only: set[str]) -> None:
        self.b = browser
        self.server = server
        self.out = out / lang
        self.lang = lang
        self.only = only
        self.sizes: dict[str, int] = {}

    def wanted(self, name: str) -> bool:
        return not self.only or name in self.only

    def url(self, path: str) -> str:
        return self.server.url + path

    def save(self, name: str, image: Image.Image) -> None:
        self.sizes[name] = pack(image, self.out / f"{name}.png")
        print(f"  {self.lang}/{name}.png  {image.width}x{image.height}  {self.sizes[name] / 1024:.1f} KB")

    # ---- how a picture is cut ----

    def frame(self, cap: int = MAX_HEIGHT, width: int = WIDTH) -> Image.Image:
        """The page with the menu, as high as its content (at most `cap`)."""
        b = self.b
        b.viewport(width, 300)
        time.sleep(0.2)
        need = b.evaluate(
            "Math.ceil(Math.max(document.querySelector('.app').getBoundingClientRect().height,"
            "document.querySelector('.sidebar').scrollHeight))"
        )
        height = min(int(need), cap)
        b.viewport(width, height)
        time.sleep(0.4)
        return b.screenshot({"x": 0, "y": 0, "width": width, "height": height})

    def element(self, selector: str, pad: int = 16, overflow: bool = False) -> Image.Image:
        """Clip to one element; with `overflow` the clip also takes in content that sticks out on the right."""
        b = self.b
        b.viewport(WIDTH, MAX_HEIGHT)
        time.sleep(0.3)
        rect = b.evaluate(
            f"(()=>{{const e=document.querySelector({json.dumps(selector)});const r=e.getBoundingClientRect();"
            "let right=r.right;"
            + ("e.querySelectorAll('*').forEach(c=>{right=Math.max(right,c.getBoundingClientRect().right)});" if overflow else "")
            + "return {x:r.left+scrollX,y:r.top+scrollY,w:right-r.left,h:r.height}})()"
        )
        x, y = max(0, rect["x"] - pad), max(0, rect["y"] - pad)
        return b.screenshot({"x": x, "y": y, "width": min(WIDTH - x, rect["w"] + 2 * pad), "height": rect["h"] + 2 * pad})

    def card(self, pad: int = 28, overflow: bool = False) -> Image.Image:
        """A page without the menu (set-up): the card with a little margin."""
        return self.element(".bare-card", pad, overflow)

    # ---- one picture ----

    def page(self, name: str, path: str, *, cap: int = MAX_HEIGHT, before: Callable[[Browser], None] | None = None,
             element: str | None = None, pad: int = 16) -> None:
        if not self.wanted(name):
            return
        self.b.viewport(WIDTH, 900)
        self.b.goto(self.url(path))
        if before:
            before(self.b)
        self.save(name, self.element(element, pad) if element else self.frame(cap))

    def mobile(self, name: str, path: str, cap: int = MAX_HEIGHT, before: Callable[[Browser], None] | None = None) -> None:
        if not self.wanted(name):
            return
        b = self.b
        b.viewport(MOBILE_WIDTH, 812, mobile=True)
        b.goto(self.url(path))
        if before:
            before(b)
        height = min(int(b.evaluate("Math.ceil(document.documentElement.scrollHeight)")), cap)
        b.viewport(MOBILE_WIDTH, height, mobile=True)
        time.sleep(0.4)
        self.save(name, b.screenshot({"x": 0, "y": 0, "width": MOBILE_WIDTH, "height": height}))
        b.call("Emulation.clearDeviceMetricsOverride")


def fill(b: Browser, **values: str) -> None:
    for name, value in values.items():
        b.evaluate(
            f"(()=>{{const e=document.querySelector('[name={name}]');e.value={json.dumps(value)};"
            "e.dispatchEvent(new Event('input',{bubbles:true}));})()"
        )


def keep_rows(b: Browser, count: int) -> None:
    """A long table: the first rows only, so no row is cut at the lower edge of the picture."""
    b.evaluate(f"document.querySelectorAll('main.content table tbody tr').forEach((r,i)=>{{if(i>={count})r.remove()}})")


def setup_flow(shot: Shooter, scn: Scenario) -> None:
    """The four steps of the first start, done in the browser; the pictures are taken on the way."""
    b, server = shot.b, shot.server
    b.viewport(WIDTH, 900)
    b.goto(shot.url("/"))  # the gate leads to /setup
    fill(b, username="admin", password="example-password", password_repeat="example-password")
    if shot.wanted("setup-admin"):
        shot.save("setup-admin", shot.card())
    b.submit("form button[type=submit]")  # lands on /setup/bambuddy/user

    b.viewport(WIDTH, 900)
    b.goto(shot.url("/setup/bambuddy/user"))
    if shot.wanted("setup-group"):
        shot.save("setup-group", shot.card())
    b.goto(shot.url("/setup/bambuddy/key"))
    if shot.wanted("setup-key"):
        shot.save("setup-key", shot.card(overflow=True))

    # step 4: first a key that lacks one right (the page lists every right with a tick or a cross) ...
    server.mock.permissions = [p for p in REQUIRED if p != "queue:start_unreviewed"]
    b.goto(shot.url("/setup/bambuddy/connect"))
    fill(b, bambuddy_address=BAMBUDDY_ADDRESS, bambuddy_token=KEY_TEXT)
    b.submit("form[action='/setup/bambuddy/connect'] button[type=submit]")
    if shot.wanted("setup-connect"):
        shot.save("setup-connect", shot.card())
    # ... then the right one, which finishes the set-up
    server.mock.permissions = [*REQUIRED, "library:upload"]
    b.goto(shot.url("/setup/bambuddy/connect"))
    fill(b, bambuddy_address=BAMBUDDY_ADDRESS, bambuddy_token=KEY_TEXT)
    b.submit("form[action='/setup/bambuddy/connect'] button[type=submit]")
    b.goto("about:blank")


def prepare_settings(server: Server) -> None:
    """Values for the settings pages that have no part in the stories above (written through the settings module)."""
    with server.sm() as s:
        st.set_setting(s, "qr_base_url", QR_ADDRESS)
        st.set_setting(s, "label_printer_host", LABEL_PRINTER)
        st.set_setting(s, "ha_enabled", True)
        st.set_setting(s, "ha_url", HOME_ASSISTANT_ADDRESS)
        st.set_setting(s, "ha_token", "example-token")
        st.set_setting(s, "ha_service", "notify.example_phone")
    # Bambuddy's live channel does not run here (no background tasks): the card says what a running one says.
    server.app.state.live_listener.state = "connected"
    # "New" on the overview (Spec 10.2, 0.4.1): the orders and the reorder suggestions were looked at
    # before the newest order and one suggestion came in, so each card shows "1 new".
    with server.sm() as s:
        open_ids = sorted(o.id for o in orders.list_orders(s, "open"))
        st.set_setting(s, "orders_seen_id", open_ids[-2] if len(open_ids) > 1 else 0)
        shown = reorder.current(s, None, at(9, 11, 30), write=False).suggestions
        seen.mark_suggestions(s, shown[1:])
        s.commit()


def take_pages(shot: Shooter, scn: Scenario) -> None:
    a = scn.articles
    cable = a["cable_guide"]

    def wait_label(b: Browser) -> None:
        b.wait_for("(()=>{const i=document.getElementById('label-preview');return !!i&&!i.hidden&&i.complete&&i.naturalWidth>0})()")

    def slim_detail(b: Browser) -> None:
        """Numbers, QR code and journal: the cards of the queue, label and settings are shown on their own pages."""
        b.evaluate(
            "document.querySelectorAll('main.content section.card.wide').forEach((s,i)=>{"
            "if(i>0&&!s.classList.contains('article-qr'))s.style.display='none'})"
        )

    def check_for_updates(b: Browser) -> None:
        b.submit("form[action='/settings/update/check'] button[type=submit]")
        b.evaluate("(()=>{const n=document.getElementById('ops-notes');if(n)n.open=true})()")  # the notes are closed at first

    shot.page("overview", "/")
    shot.page("articles-list", "/articles?layout=tiles")
    shot.page("article-new-files", f"/articles/new?folder={scn.folder}")
    shot.page("article-new-plates", f"/articles/new/file/{scn.files['Spool holder']}")
    shot.page("article-new-form", f"/articles/new/file/{scn.files['Label holder']}/plate/1", cap=1200)
    shot.page("article-upload", f"/articles/new/upload?folder={scn.folder}")
    shot.page("article-filaments", f"/articles/{a['bracket_small']}/filaments")
    shot.page("article-detail", f"/articles/{cable}", cap=1400, before=slim_detail)
    shot.page("queue", "/queue", cap=1350, before=lambda b: b.evaluate("document.getElementById('manual').style.display='none'"))  # the form is not the point here
    shot.page("inspection", "/inspection")
    shot.page("assembly", "/assembly")
    shot.page("label-preview", f"/articles/{cable}", before=wait_label, element="#label")
    shot.page("reorder", "/reorder")
    shot.page("filament-page", "/filament")
    # the sort with two suppliers (Spec 9.3): the block of suppliers of the reorder settings
    shot.page("filament-settings", f"/filament/{scn.sorts['petg_black']}")
    shot.page("orders", "/orders")
    shot.page("parts", "/parts")
    shot.page("sets", "/sets")
    shot.page("set-form", f"/sets/{scn.sets['Bracket kit']}/edit", cap=1300)
    shot.page("assembled-form", f"/articles/{a['hose']}/assembled", cap=1500)
    shot.page("assembled-detail", f"/articles/{a['hose']}", cap=1100)
    shot.page("bookings", "/bookings", cap=990)  # the four forms; the fifth would be cut
    shot.page("journal", "/journal", before=lambda b: keep_rows(b, 15))
    shot.page("settings-bambuddy", "/settings?tab=bambuddy")
    shot.page("settings-automation", "/settings?tab=automation")
    shot.page("settings-notifications", "/settings?tab=notify", cap=1400)
    shot.page("settings-labels", "/settings?tab=labels")
    shot.page("settings-log", "/settings?tab=log", before=lambda b: keep_rows(b, 14))
    shot.mobile(
        "articles-mobile", "/articles?layout=tiles", cap=1250,  # two tiles: none is cut at the lower edge
        before=lambda b: b.evaluate("document.querySelectorAll('.tiles .tile').forEach((r,i)=>{if(i>=2)r.remove()})"),
    )
    shot.mobile("overview-mobile", "/", cap=1900)  # the print plan as a list per printer (Spec 10.2, 0.4.2)
    # last: a found update puts a bar at the top of every page after it
    shot.page("settings-update", "/settings?tab=maintenance", before=check_for_updates, cap=1300)


def run_language(lang: str, out: Path, only: set[str]) -> dict[str, int]:
    work = Path(tempfile.mkdtemp(prefix="reprintbuddy-shots-"))
    LANGUAGE["now"] = lang
    CLOCK.set(at(2, 8))
    server = Server(work, lang)
    scn = Scenario()
    fill_bambuddy(server.fake, scn)
    server.start()
    browser = Browser(work, lang)
    try:
        shot = Shooter(browser, server, out, lang, only)
        setup_flow(shot, scn)
        build_catalog(server, scn)
        build_history(server, scn)
        run_prints(server, scn)
        prepare_settings(server)
        CLOCK.set(at(9, 11, 30))
        # The printers' state as a live message would have brought it just now: the print plan of the
        # pictures shows no "as of" hint (Spec 10.2, 0.4.2).
        server.app.state.printer_board.at = CLOCK()
        take_pages(shot, scn)
        return shot.sizes
    finally:
        browser.close()
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "assets" / "images")
    parser.add_argument("--only", nargs="+", metavar="NAME", choices=ALL_NAMES, default=[])
    parser.add_argument("--lang", choices=("en", "de"))
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress lines appear at once
    freeze_application_clock()
    started = time.monotonic()
    sizes: dict[str, int] = {}
    for lang in ([args.lang] if args.lang else ["en", "de"]):
        print(f"language {lang}")
        for name, size in run_language(lang, args.out, set(args.only)).items():
            sizes[f"{lang}/{name}"] = size
    total = sum(sizes.values())
    print(f"{len(sizes)} pictures, {total / 1024:.0f} KB together, {time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
