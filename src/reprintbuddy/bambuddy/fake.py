"""In-memory stand-in for Bambuddy, used by every test (Spec 12)."""

from __future__ import annotations

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
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


class FakeBambuddy:
    def __init__(self) -> None:
        self.unavailable = False
        self.calls: list[str] = []
        self._folders: list[LibraryFolder] = []
        self._files: dict[int, LibraryFile] = {}
        self._plates: dict[int, int] = {}
        self._reqs: dict[tuple[int, int], list[FilamentRequirement]] = {}
        self._printers: list[Printer] = []
        self._next_folder = 1
        self._next_file = 1

    # ---- fill ------------------------------------------------------------
    def add_folder(self, name: str, parent_id: int | None = None) -> LibraryFolder:
        folder = LibraryFolder(self._next_folder, name, parent_id)
        self._next_folder += 1
        self._folders.append(folder)
        return folder

    def add_file(
        self,
        filename: str,
        folder_id: int | None = None,
        *,
        printer_model: str | None = "A1",
        plates: int = 1,
        fingerprint: str = "v1",
    ) -> LibraryFile:
        file = LibraryFile(self._next_file, filename, folder_id, fingerprint, printer_model)
        self._next_file += 1
        self._files[file.id] = file
        self._plates[file.id] = plates
        return file

    def set_requirements(
        self, file_id: int, plate_index: int, reqs: list[FilamentRequirement]
    ) -> None:
        self._reqs[(file_id, plate_index)] = list(reqs)

    def remove_file(self, file_id: int) -> None:
        self._files.pop(file_id, None)
        self._plates.pop(file_id, None)

    def add_printer(self, name: str, model: str) -> Printer:
        printer = Printer(len(self._printers) + 1, name, model)
        self._printers.append(printer)
        return printer

    # ---- the interface -----------------------------------------------------
    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.unavailable:
            raise BambuddyUnavailable("fake Bambuddy is switched off")

    async def ping(self) -> bool:
        self.calls.append("ping")
        return not self.unavailable

    async def list_folders(self) -> list[LibraryFolder]:
        self._enter("list_folders")
        return list(self._folders)

    async def list_files(self, folder_id: int | None) -> list[LibraryFile]:
        self._enter("list_files")
        return [f for f in self._files.values() if f.folder_id == folder_id]

    async def get_file(self, file_id: int) -> LibraryFile | None:
        self._enter("get_file")
        return self._files.get(file_id)

    async def list_plates(self, file_id: int) -> list[Plate]:
        self._enter("list_plates")
        if file_id not in self._files:
            raise BambuddyRejected(f"unknown file {file_id}")
        return [Plate(i, None) for i in range(1, self._plates[file_id] + 1)]

    async def plate_thumbnail(self, file_id: int, plate_index: int) -> bytes:
        self._enter("plate_thumbnail")
        if file_id not in self._files or not 1 <= plate_index <= self._plates[file_id]:
            raise BambuddyRejected(f"unknown plate {file_id}:{plate_index}")
        return b"\x89PNG\r\n\x1a\n" + f"{file_id}:{plate_index}".encode()

    async def filament_requirements(
        self, file_id: int, plate_id: int
    ) -> list[FilamentRequirement]:
        self._enter("filament_requirements")
        return list(self._reqs.get((file_id, plate_id), []))

    async def list_printers(self) -> list[Printer]:
        self._enter("list_printers")
        return list(self._printers)

    # ---- left to B2 --------------------------------------------------------
    async def ensure_batch(self, name: str, external_ref: str, library_file_id: int) -> int:
        self.calls.append("ensure_batch")
        raise NotImplementedError

    async def enqueue(self, req: EnqueueRequest) -> QueueItem:
        self.calls.append("enqueue")
        raise NotImplementedError

    async def get_queue_item(self, item_id: int) -> QueueItem | None:
        self.calls.append("get_queue_item")
        raise NotImplementedError

    async def list_queue(self) -> list[QueueItem]:
        self.calls.append("list_queue")
        raise NotImplementedError

    async def find_items_by_batch(self, batch_id: int) -> list[QueueItem]:
        self.calls.append("find_items_by_batch")
        raise NotImplementedError

    async def list_spools(self) -> list[Spool]:
        self.calls.append("list_spools")
        raise NotImplementedError
