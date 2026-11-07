"""The interface to Bambuddy, its error classes and the HTTP client (B2)."""

from __future__ import annotations

import json
import logging
from contextlib import AbstractAsyncContextManager
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from reprintbuddy.bambuddy.connection import TIMEOUT_SECONDS, ConnectionCheck, _evaluate
from reprintbuddy.bambuddy.types import (
    EnqueueRequest,
    FilamentRequirement,
    LibraryFile,
    LibraryFolder,
    Plate,
    Printer,
    QueueItem,
    Spool,
    SpoolMode,
)

log = logging.getLogger(__name__)


class BambuddyUnavailable(Exception):
    """Network error, 5xx, timeout or an invalid address: nothing is known about the outcome."""


class BambuddyUnexpected(BambuddyUnavailable):
    """An answer that does not fit Bambuddy's source: no JSON, wrong form, 3xx.

    A subclass of `BambuddyUnavailable`, so every caller treats it as "outcome unclear".
    """


class BambuddyRejected(Exception):
    """Bambuddy answered with a 4xx.

    `detail` is Bambuddy's `detail` (as JSON text when it is not a text), `status` the code.
    `BambuddyRejected("…")` without a status stays valid.
    """

    def __init__(self, detail: str = "", *, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status

    def __str__(self) -> str:
        return self.detail if self.status is None else f"{self.status}: {self.detail}"


# After the error classes: `parse` imports them from here.
from reprintbuddy.bambuddy import parse as _parse  # noqa: E402

_PNG_HEAD = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])

# Preview images of library files: Bambuddy names the type by the file extension
# (`library.py:5794-5831`), so it is not always PNG. Declared type and magic bytes must agree.
_IMAGE_KINDS = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _image_kind_of(content: bytes) -> str | None:
    if content.startswith(_PNG_HEAD):
        return "image/png"
    if content.startswith(bytes([0xFF, 0xD8, 0xFF])):
        return "image/jpeg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


HttpFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


@runtime_checkable
class BambuddyClient(Protocol):
    async def ping(self) -> bool: ...

    async def list_folders(self) -> list[LibraryFolder]: ...

    async def list_files(self, folder_id: int | None) -> list[LibraryFile]: ...

    async def get_file(self, file_id: int) -> LibraryFile | None: ...

    async def list_plates(self, file_id: int) -> list[Plate]: ...

    async def plate_thumbnail(self, file_id: int, plate_index: int) -> bytes: ...

    async def file_thumbnail(self, file_id: int) -> tuple[str, bytes]: ...

    async def filament_requirements(
        self, file_id: int, plate_id: int
    ) -> list[FilamentRequirement]: ...

    async def enqueue(self, req: EnqueueRequest) -> QueueItem: ...

    async def get_queue_item(self, item_id: int) -> QueueItem | None: ...

    async def list_queue(self) -> list[QueueItem]: ...

    async def list_all_items(self) -> list[QueueItem]: ...

    async def list_spools(self) -> list[Spool]: ...

    async def list_printers(self) -> list[Printer]: ...

    async def spool_mode(self) -> SpoolMode: ...


BambuddyFactory = Callable[[], BambuddyClient | None]


def _detail(response: httpx.Response) -> str:
    """Bambuddy's error text: `detail` as it is, or as JSON text; `""` without one."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict) or "detail" not in body:
        return ""
    detail = body["detail"]
    return detail if isinstance(detail, str) else json.dumps(detail)


class HttpBambuddyClient:
    """Talks to Bambuddy's REST API (`<address>/api/v1`).

    Every method opens its own `httpx.AsyncClient` through `http` (Decision 4). The key goes
    only into the header `X-API-Key`; it is never part of a URL, a log line, an error
    message or `repr`. Redirects are never followed, so the key cannot travel elsewhere.
    """

    def __init__(self, base_url: str, api_key: str, http: HttpFactory) -> None:
        self._base_url = base_url.rstrip("/")
        self._api = f"{self._base_url}/api/v1"
        self._api_key = api_key
        self._http = http

    def __repr__(self) -> str:
        return f"HttpBambuddyClient({self._base_url})"

    # ---- transport -----------------------------------------------------------------
    async def _send(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        """One call; network errors and status codes become the error classes.

        Returns the response for 2xx and 404 (the caller decides about 404).
        """
        try:
            async with self._http() as http:
                response = await http.request(
                    method,
                    f"{self._api}{path}",
                    params=params,
                    json=body,
                    headers={"X-API-Key": self._api_key},
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx' messages name the URL at most, never the headers.
            log.debug("Bambuddy %s %s failed: %s", method, path, type(exc).__name__)
            raise BambuddyUnavailable(f"{method} {path}: {type(exc).__name__}") from exc

        status = response.status_code
        log.debug("Bambuddy %s %s -> %s", method, path, status)
        if status >= 500:
            raise BambuddyUnavailable(f"{method} {path}: HTTP {status}")
        if 300 <= status < 400:
            raise BambuddyUnexpected(f"{method} {path}: redirect HTTP {status} not followed")
        if 400 <= status < 500 and status != 404:
            raise BambuddyRejected(_detail(response), status=status)
        if status != 404 and not 200 <= status < 300:
            raise BambuddyUnexpected(f"{method} {path}: HTTP {status}")
        return response

    @staticmethod
    def _json(response: httpx.Response, method: str, path: str) -> object:
        try:
            data = response.json()
        except ValueError:
            raise BambuddyUnexpected(f"{method} {path}: answer is not JSON") from None
        except RecursionError:
            # A pathologically deep body is no answer of the source (not a ValueError).
            raise BambuddyUnexpected(f"{method} {path}: answer is nested too deeply") from None
        if data is None:
            # `None` from `_get` means "gone" (404); a JSON `null` is no answer of the source.
            raise BambuddyUnexpected(f"{method} {path}: answer is empty")
        return data

    async def _get(self, path: str, params: dict | None = None, *, none_on_404: bool = False) -> object | None:
        response = await self._send("GET", path, params=params)
        if response.status_code == 404:
            if none_on_404:
                return None
            raise BambuddyRejected(_detail(response), status=404)
        return self._json(response, "GET", path)

    async def _get_bytes(self, path: str) -> tuple[str, bytes]:
        """(content type, content) of a binary answer, e.g. a plate image."""
        response = await self._send("GET", path)
        if response.status_code == 404:
            raise BambuddyRejected(_detail(response), status=404)
        return response.headers.get("content-type", ""), response.content

    async def _post(self, path: str, body: dict | None = None) -> object:
        """A POST; without `body` the request has no body at all."""
        response = await self._send("POST", path, body=body)
        if response.status_code == 404:
            raise BambuddyRejected(_detail(response), status=404)
        return self._json(response, "POST", path)

    # ---- the interface -----------------------------------------------------------------
    async def list_folders(self) -> list[LibraryFolder]:
        return _parse.parse_folders(await self._get("/library/folders/"))

    async def list_files(self, folder_id: int | None) -> list[LibraryFile]:
        """`None`: the top level only; a number: that folder, not its sub-folders (Decision 1)."""
        params = None if folder_id is None else {"folder_id": folder_id}
        return _parse.parse_files(await self._get("/library/files/", params))

    async def get_file(self, file_id: int) -> LibraryFile | None:
        """`None`: the file is gone."""
        data = await self._get(f"/library/files/{file_id}", none_on_404=True)
        return None if data is None else _parse.parse_file(data)

    async def list_plates(self, file_id: int) -> list[Plate]:
        return _parse.parse_plates(await self._get(f"/library/files/{file_id}/plates"))

    async def plate_thumbnail(self, file_id: int, plate_index: int) -> bytes:
        """The plate image as PNG bytes; anything else with a 2xx is unexpected."""
        path = f"/library/files/{file_id}/plate-thumbnail/{plate_index}"
        content_type, content = await self._get_bytes(path)
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind not in ("", "image/png") or not content.startswith(_PNG_HEAD):
            raise BambuddyUnexpected(f"GET {path}: answer is no PNG image")
        return content

    async def file_thumbnail(self, file_id: int) -> tuple[str, bytes]:
        """(content type, bytes) of the file's preview; 404 is `BambuddyRejected`."""
        path = f"/library/files/{file_id}/thumbnail"
        content_type, content = await self._get_bytes(path)
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind not in _IMAGE_KINDS or _image_kind_of(content) != kind:
            raise BambuddyUnexpected(f"GET {path}: answer is no supported image")
        return kind, content

    async def filament_requirements(self, file_id: int, plate_id: int) -> list[FilamentRequirement]:
        data = await self._get(
            f"/library/files/{file_id}/filament-requirements", {"plate_id": plate_id}
        )
        return _parse.parse_requirements(data)

    async def list_printers(self) -> list[Printer]:
        """Every printer with a model; `active` is False for one in maintenance."""
        return _parse.parse_printers(await self._get("/printers/"))

    async def spool_mode(self) -> SpoolMode:
        """Which spool management Bambuddy runs right now (Decision 17)."""
        return _parse.parse_spool_mode(await self._get("/spoolman/status"))

    async def list_spools(self) -> list[Spool]:
        """The spools of the running mode, and only of it (Decision 12).

        An unreachable Spoolman or a refused status is an error, never a reason to read the
        other list: its numbers would be stale or foreign.
        """
        mode = await self.spool_mode()
        path = "/spoolman/inventory/spools" if mode == "spoolman" else "/inventory/spools"
        return _parse.parse_spools(await self._get(path))

    async def get_queue_item(self, item_id: int) -> QueueItem | None:
        """`None`: the entry is gone (deleted, or removed with the history; Decision 3)."""
        data = await self._get(f"/queue/{item_id}", none_on_404=True)
        return None if data is None else _parse.parse_queue_item(data)

    async def list_queue(self) -> list[QueueItem]:
        """The open entries (pending, printing), in Bambuddy's order (by position)."""
        return _parse.parse_queue_items(await self._get("/queue/", {"status": "pending,printing"}))

    async def list_all_items(self) -> list[QueueItem]:
        """Every entry Bambuddy shows the key's owner, history included, in Bambuddy's order.

        `GET /queue/` without `status` (Bambuddy `print_queue.py:740-806`, sorted by position,
        then id). The search after an unclear hand-over needs the history too: the print may
        already be finished or cancelled (Spec 7.6).
        """
        return _parse.parse_queue_items(await self._get("/queue/"))

    # ---- the one write call (Spec 3.1; `tests/bambuddy/test_write_guard.py` watches) ----
    async def enqueue(self, req: EnqueueRequest) -> QueueItem:
        """One single queue entry, sent once (Decision 11, Review Focus 1; Spec 7.6).

        The body holds exactly the file, the plate, one target (model or printer) and
        `manual_start: false`; `insert_at_top` only when set, `insert_position` only when
        given. No `batch_id` and no `quantity`: Bambuddy then makes exactly one entry outside
        any batch (`print_queue.py:1347-1408`). No print options: Bambuddy's defaults apply.
        A request with both targets, none, an empty model or a position below 1 is a
        `ValueError` before any network call. A timeout is `BambuddyUnavailable` with an
        unknown outcome; the caller looks for the entry (`list_all_items`) instead of
        sending it again.
        """
        if req.target_model == "":
            raise ValueError("target_model is empty")
        if (req.target_model is None) == (req.printer_id is None):
            raise ValueError("exactly one of target_model and printer_id is needed")
        if req.insert_position is not None and req.insert_position < 1:
            raise ValueError("insert_position starts at 1")
        body: dict = {"library_file_id": req.library_file_id, "plate_id": req.plate_id}
        if req.target_model is not None:
            body["target_model"] = req.target_model
        else:
            body["printer_id"] = req.printer_id
        body["manual_start"] = False
        if req.insert_at_top:
            body["insert_at_top"] = True
        if req.insert_position is not None:
            body["insert_position"] = req.insert_position
        return _parse.parse_queue_item(await self._post("/queue/", body))

    # ---- the live channel (Spec 7.6; not in the Protocol, B4's interface stays as it is) ----
    async def ws_token(self) -> str:
        """A token for Bambuddy's live channel, valid 60 minutes (B2 Task 9).

        The second POST and no write: it changes nothing in the queue, the printers or the
        library. It needs the group right `websocket:connect`; without it Bambuddy answers
        403 (`BambuddyRejected`). An answer without a non-empty text `token` is
        `BambuddyUnexpected`. The token is never part of an error message.
        """
        data = await self._post("/auth/ws-token")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise BambuddyUnexpected("POST /auth/ws-token: answer has no token")
        return token

    def ws_url(self, token: str) -> str:
        """The live channel's address: `ws://…/api/v1/ws?token=…` (`wss://` for https)."""
        scheme, sep, rest = self._api.partition("://")
        secure = {"http": "ws", "https": "wss"}.get(scheme.lower())
        if not sep or secure is None:
            raise ValueError("the Bambuddy address is no http or https address")
        return f"{secure}://{rest}/ws?token={quote(token, safe='')}"

    async def check_connection(self) -> ConnectionCheck:
        """Who owns the key and may it do everything Reprintbuddy needs? Maps the errors below.

        Only the `BambuddyUnavailable` family (and `BambuddyRejected`) is mapped to a result; any
        other exception (e.g. a key that cannot be sent as a header, a closed client) propagates.
        `ping()` is the method that never raises.

        Mapping as in B1: network error, timeout, invalid address and 5xx are `unreachable`;
        401 `unauthorized`; 403 `forbidden`; 200 goes to `_evaluate`; everything else (HTML,
        no JSON, 3xx, 404, any other status) is `not_bambuddy`. `BambuddyUnexpected` is caught
        before `BambuddyUnavailable`, its parent (Decision 5).
        """
        try:
            response = await self._send("GET", "/auth/me")
            if response.status_code != 200:
                # 404 and any other 2xx: B1 and the brief send only 200 to `_evaluate`.
                return ConnectionCheck(False, "not_bambuddy")
            body = self._json(response, "GET", "/auth/me")
        except BambuddyRejected as exc:
            if exc.status == 401:
                return ConnectionCheck(False, "unauthorized")
            if exc.status == 403:
                return ConnectionCheck(False, "forbidden")
            return ConnectionCheck(False, "not_bambuddy")
        except BambuddyUnexpected:
            return ConnectionCheck(False, "not_bambuddy")
        except BambuddyUnavailable:
            return ConnectionCheck(False, "unreachable")
        return _evaluate(body)

    async def ping(self) -> bool:
        """True when Bambuddy answers and accepts the key; missing rights do not matter here.

        Never raises: anything unexpected (e.g. a key that cannot be sent as a header, a closed
        client from the factory) is False. Only the type is logged, never the key. `CancelledError`
        is a `BaseException`, so it still propagates.
        """
        try:
            return (await self.check_connection()).reason in ("ok", "missing_permissions")
        except Exception as exc:
            log.debug("Bambuddy ping failed: %s", type(exc).__name__)
            return False
