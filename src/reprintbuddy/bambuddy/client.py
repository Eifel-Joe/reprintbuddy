"""The interface to Bambuddy, its error classes and the HTTP client (B2)."""

from __future__ import annotations

import json
import logging
from contextlib import AbstractAsyncContextManager
from typing import Callable, Protocol, runtime_checkable

import httpx

from reprintbuddy.bambuddy.connection import TIMEOUT_SECONDS
from reprintbuddy.bambuddy.types import (
    EnqueueRequest,
    FilamentRequirement,
    LibraryFile,
    LibraryFolder,
    Plate,
    Printer,
    QueueItem,
    Spool,
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


class BatchRefLost(BambuddyRejected):
    """409 for the batch reference, but no batch with it can be found (Decision 10)."""


# After the error classes: `parse` imports them from here.
from reprintbuddy.bambuddy import parse as _parse  # noqa: E402

_PNG_HEAD = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])

HttpFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


@runtime_checkable
class BambuddyClient(Protocol):
    async def ping(self) -> bool: ...

    async def list_folders(self) -> list[LibraryFolder]: ...

    async def list_files(self, folder_id: int | None) -> list[LibraryFile]: ...

    async def get_file(self, file_id: int) -> LibraryFile | None: ...

    async def list_plates(self, file_id: int) -> list[Plate]: ...

    async def plate_thumbnail(self, file_id: int, plate_index: int) -> bytes: ...

    async def filament_requirements(
        self, file_id: int, plate_id: int
    ) -> list[FilamentRequirement]: ...

    async def ensure_batch(self, name: str, external_ref: str, library_file_id: int) -> int: ...

    async def enqueue(self, req: EnqueueRequest) -> QueueItem: ...

    async def get_queue_item(self, item_id: int) -> QueueItem | None: ...

    async def list_queue(self) -> list[QueueItem]: ...

    async def find_items_by_batch(self, batch_id: int) -> list[QueueItem]: ...

    async def list_spools(self) -> list[Spool]: ...

    async def list_printers(self) -> list[Printer]: ...


BambuddyFactory = Callable[[], BambuddyClient]


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

    async def _post(self, path: str, body: dict) -> object:
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

    async def filament_requirements(self, file_id: int, plate_id: int) -> list[FilamentRequirement]:
        data = await self._get(
            f"/library/files/{file_id}/filament-requirements", {"plate_id": plate_id}
        )
        return _parse.parse_requirements(data)

    async def list_printers(self) -> list[Printer]:
        """Every printer with a model; `active` is False for one in maintenance."""
        return _parse.parse_printers(await self._get("/printers/"))

    async def get_queue_item(self, item_id: int) -> QueueItem | None:
        """`None`: the entry is gone (deleted, or removed with the history; Decision 3)."""
        data = await self._get(f"/queue/{item_id}", none_on_404=True)
        return None if data is None else _parse.parse_queue_item(data)
