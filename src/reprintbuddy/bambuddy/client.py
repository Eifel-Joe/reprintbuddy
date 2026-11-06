"""The interface to Bambuddy. There is no HTTP implementation in this module yet (B2)."""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

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
