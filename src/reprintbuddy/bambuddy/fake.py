"""In-memory stand-in for Bambuddy, used by every test (Spec 12)."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import zipfile
from collections.abc import Callable
from typing import BinaryIO
from datetime import UTC, datetime

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import (
    EnqueueRequest,
    FilamentRequirement,
    LibraryFile,
    LibraryFolder,
    Plate,
    PrintOptions,
    Printer,
    QueueItem,
    Spool,
    SpoolMode,
    UploadedFile,
)

# The methods with an effect (Spec 3.1, 5.1a): `fail_next(..., after_effect=True)` lets the
# effect happen first ("the answer got lost"). Every other method has no effect, so it fails
# at once.
_EFFECT_METHODS = frozenset({"enqueue", "upload_file"})
_FAILABLE_METHODS = _EFFECT_METHODS | {
    "list_folders", "list_files", "get_file", "list_plates", "plate_thumbnail", "file_thumbnail",
    "filament_requirements", "list_printers", "spool_mode", "list_spools", "print_defaults",
    "get_queue_item", "list_queue", "list_all_items",
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Bambuddy's `is_gcode_compatible` (backend/app/utils/printer_models.py): a file sliced for one model
# may go to another only when both are of the same family; a missing declaration passes. The map
# turns the internal codes a file can carry into the short names.
_MODEL_CODES = {
    "BL-P001": "X1C", "BL-P002": "X1", "C13": "X1E", "C11": "P1P", "C12": "P1S", "P1P": "P1P",
    "P1S": "P1S", "N7": "P2S", "P2S": "P2S", "N6": "X2D", "N9": "A2L", "A11": "A1",
    "A12": "A1 Mini", "N1": "A1 Mini", "N2S": "A1", "A04": "A1 Mini", "O1D": "H2D",
    "O1E": "H2D Pro", "O2D": "H2D Pro", "O1C": "H2C", "O1C2": "H2C", "O1S": "H2S",
}
_MODEL_FAMILIES = (frozenset({"X1", "X1C", "X1E", "P1P", "P1S"}),)


def _gcode_compatible(sliced_for: str | None, target: str) -> bool:
    if not sliced_for or not target:
        return True

    def short(model: str) -> str:
        return _MODEL_CODES.get(model.strip(), model).strip().upper().replace(" ", "").replace("-", "")

    a, b = short(sliced_for), short(target)
    return a == b or any(a in family and b in family for family in _MODEL_FAMILIES)


class FakeBambuddy:
    def __init__(self) -> None:
        self.unavailable = False
        self.spoolman_enabled = False
        self.spoolman_reachable = True  # False + spoolman_enabled: list_spools raises
        self.delay: float = 0.0  # queue calls wait first, so other tasks can interleave
        self.username = "reprintbuddy"  # Bambuddy's `created_by_username` of every own entry
        # Bambuddy's clock: `created_at` of new entries (naive UTC). Tests set it to place an
        # entry in time against `enqueue_sent_at` (Spec 7.6).
        self.now: Callable[[], datetime] = _utcnow
        self.enqueued: list[EnqueueRequest] = []
        # Bambuddy's "Default print options" (p342, 0.4.0): its install defaults
        # (`schemas/settings.py`) until a test sets others; `options` keeps what each entry got.
        self.defaults = PrintOptions("auto", "auto", "auto", True, False, False)
        self.options: dict[int, PrintOptions] = {}
        self.calls: list[str] = []
        self._folders: list[LibraryFolder] = []
        self._files: dict[int, LibraryFile] = {}
        self._plates: dict[int, int] = {}
        self._reqs: dict[tuple[int, int], list[FilamentRequirement]] = {}
        self._printers: list[Printer] = []
        self._next_folder = 1
        self._next_file = 1
        self._items: dict[int, QueueItem] = {}
        self._next_item = 1
        self._failures: dict[str, tuple[Exception, bool]] = {}
        self._spools: dict[int, Spool] = {}
        self._spool_lists: dict[int, SpoolMode] = {}  # which of Bambuddy's two lists holds it
        self._next_spool = 1
        # Uploading (Spec 5.1a): the key may ("Manage Library" + `library:upload`), what came in,
        # each uploaded file's content hash (Bambuddy's `duplicate_of`), read-only mounts.
        self.can_upload = True
        self.uploaded: list[tuple[int | None, str, bytes]] = []
        self._hashes: dict[int, str] = {}
        self._readonly: set[int] = set()

    # ---- fill ------------------------------------------------------------
    def add_folder(
        self, name: str, parent_id: int | None = None, *, external: bool = False, readonly: bool = False
    ) -> LibraryFolder:
        folder = LibraryFolder(self._next_folder, name, parent_id, is_external=external)
        self._next_folder += 1
        self._folders.append(folder)
        if readonly:
            self._readonly.add(folder.id)
        return folder

    def add_file(
        self,
        filename: str,
        folder_id: int | None = None,
        *,
        printer_model: str | None = "A1",
        plates: int = 1,
        fingerprint: str = "v1",
        has_thumbnail: bool = True,
        sliced: bool = True,
    ) -> LibraryFile:
        file = LibraryFile(
            self._next_file, filename, folder_id, fingerprint, printer_model, has_thumbnail, sliced
        )
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

    def add_printer(self, name: str, model: str, *, active: bool = True) -> Printer:
        printer = Printer(len(self._printers) + 1, name, model, active)
        self._printers.append(printer)
        return printer

    # ---- the interface -----------------------------------------------------
    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.unavailable:
            raise BambuddyUnavailable("fake Bambuddy is switched off")
        if name not in _EFFECT_METHODS:  # the writing method handles its failure itself
            failure = self._failures.pop(name, None)
            if failure:
                raise failure[0]

    async def ping(self) -> bool:
        self.calls.append("ping")
        return not self.unavailable

    async def list_folders(self) -> list[LibraryFolder]:
        self._enter("list_folders")
        return list(self._folders)

    async def list_files(self, folder_id: int | None) -> list[LibraryFile]:
        self._enter("list_files")
        # Bambuddy lists a folder ordered by file name (`order_by(LibraryFile.filename)`).
        return sorted((f for f in self._files.values() if f.folder_id == folder_id), key=lambda f: f.filename)

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

    async def file_thumbnail(self, file_id: int) -> tuple[str, bytes]:
        self._enter("file_thumbnail")
        file = self._files.get(file_id)
        if file is None or not file.has_thumbnail:
            raise BambuddyRejected(f"no thumbnail for file {file_id}", status=404)
        return "image/png", b"\x89PNG\r\n\x1a\n" + f"file:{file_id}".encode()

    async def filament_requirements(
        self, file_id: int, plate_id: int
    ) -> list[FilamentRequirement]:
        self._enter("filament_requirements")
        return list(self._reqs.get((file_id, plate_id), []))

    async def list_printers(self) -> list[Printer]:
        self._enter("list_printers")
        return list(self._printers)

    async def print_defaults(self) -> PrintOptions:
        self._enter("print_defaults")
        return self.defaults

    async def spool_mode(self) -> SpoolMode:
        self._enter("spool_mode")
        return "spoolman" if self.spoolman_enabled else "builtin"


    # ---- spools: two lists, as Bambuddy keeps them (Decision 12) -----------
    def add_spool(
        self,
        material: str,
        *,
        in_mode: SpoolMode = "builtin",
        brand: str | None = "Example Brand",
        subtype: str | None = None,
        color_name: str | None = None,
        color_hex: str | None = "#000000",
        label_g: int = 1000,
        remaining_g: float = 1000.0,
        archived: bool = False,
        created_at: datetime | None = None,
    ) -> Spool:
        """Put a spool into the list of `in_mode`; `list_spools` shows only the running mode's."""
        spool = Spool(
            self._next_spool, material, subtype, brand, color_name, color_hex,
            label_g, remaining_g, archived, created_at,
        )
        self._next_spool += 1
        self._spools[spool.id] = spool
        self._spool_lists[spool.id] = in_mode
        return spool

    def set_remaining(self, spool_id: int, grams: float) -> None:
        self._spools[spool_id] = dataclasses.replace(self._spools[spool_id], remaining_g=grams)

    def archive_spool(self, spool_id: int) -> None:
        self._spools[spool_id] = dataclasses.replace(self._spools[spool_id], archived=True)

    async def list_spools(self) -> list[Spool]:
        self._enter("list_spools")
        # The mode is read directly: the real client's inner `spool_mode` call is not one of
        # the caller's calls, and the recorded `calls` stay what a caller made.
        mode: SpoolMode = "spoolman" if self.spoolman_enabled else "builtin"
        if mode == "spoolman" and not self.spoolman_reachable:
            raise BambuddyUnavailable("Spoolman server is not reachable")
        return [
            spool
            for spool_id, spool in sorted(self._spools.items())
            if self._spool_lists[spool_id] == mode  # archived ones too, as the real client asks for them
        ]

    # ---- print queue -------------------------------------------------------
    def fail_next(self, method: str, exc: Exception, *, after_effect: bool = False) -> None:
        """The next call of `method` raises `exc`; with `after_effect` the effect happens first.

        `after_effect` ("the answer got lost") only matters for the writing method (`enqueue`);
        a read has no effect, so it raises at once either way.
        """
        if method not in _FAILABLE_METHODS:
            raise ValueError(f"cannot inject a failure into {method!r}")
        self._failures[method] = (exc, after_effect)

    def _pending(self) -> list[QueueItem]:
        return [i for i in self._items.values() if i.status == "pending"]

    def _highest(self) -> int:
        # One sequence over all pending entries, whatever printer they are pinned to: Bambuddy's
        # `max_queue_position` (`services/queue_position.py`, #3200).
        return max((i.position or 0 for i in self._pending()), default=0)

    def _put(self, item: QueueItem) -> None:
        self._items[item.id] = item

    def _new_item(self, **fields: object) -> QueueItem:
        values: dict[str, object] = {
            "status": "pending", "batch_id": None, "position": None, "printer_id": None,
            "target_model": None, "label": "", "created_by": None, "library_file_id": None,
            "plate_id": None, "printer_name": None, "manual_start": False,
            "waiting_reason": None, "started_at": None, "completed_at": None,
            "error_message": None, "created_at": self.now(),
        }
        values.update(fields)
        item = QueueItem(id=self._next_item, **values)  # type: ignore[arg-type]
        self._next_item += 1
        self._put(item)
        return item

    def add_foreign_item(
        self,
        label: str = "Other print",
        *,
        status: str = "pending",
        created_by: str = "admin",
        library_file_id: int | None = None,
        plate_id: int | None = None,
        created_at: datetime | None = None,
        undated: bool = False,
    ) -> QueueItem:
        """An entry made in Bambuddy without `enqueue`, at the end of the queue.

        By default somebody else's ("admin"). With `created_by=fake.username`, a file, a plate
        and a time it stands for an own entry Reprintbuddy does not know (Spec 7.6): e.g. one
        from an earlier lost answer. `created_at` defaults to the fake's clock; `undated`
        leaves it empty, as an answer without the field would.
        """
        position = self._highest() + 1 if status == "pending" else 0  # Bambuddy defaults to 0
        return self._new_item(
            label=label,
            status=status,
            created_by=created_by,
            position=position,
            library_file_id=library_file_id,
            plate_id=plate_id,
            created_at=None if undated else (created_at or self.now()),
        )

    def set_status(self, item_id: int, status: str, *, printer_id: int | None = None) -> None:
        item = self._items[item_id]
        now = datetime.now(UTC).replace(tzinfo=None)
        changes: dict[str, object] = {"status": status}
        if printer_id is not None:
            changes["printer_id"] = printer_id
            printer = next((p for p in self._printers if p.id == printer_id), None)
            changes["printer_name"] = printer.name if printer else None
        if status == "printing":
            changes["started_at"] = now
        elif status in ("completed", "failed", "skipped", "cancelled"):
            changes["completed_at"] = now
        self._put(dataclasses.replace(item, **changes))

    def set_manual_start(self, item_id: int, value: bool) -> None:
        self._put(dataclasses.replace(self._items[item_id], manual_start=value))

    def delete_item(self, item_id: int) -> None:
        """What deleting an entry in Bambuddy does: it is gone, the positions keep their gaps."""
        self._items.pop(item_id, None)

    def update_file(
        self, file_id: int, *, filename: str | None = None, fingerprint: str | None = None
    ) -> None:
        changes: dict[str, object] = {}
        if filename is not None:
            changes["filename"] = filename
        if fingerprint is not None:
            changes["fingerprint"] = fingerprint
        self._files[file_id] = dataclasses.replace(self._files[file_id], **changes)

    async def _write_call(self, name: str) -> tuple[Exception, bool] | None:
        """Start of a writing call; returns the queued failure that must fire after the effect."""
        await asyncio.sleep(self.delay)
        self._enter(name)
        failure = self._failures.pop(name, None)
        if failure and not failure[1]:
            raise failure[0]
        return failure

    async def enqueue(self, req: EnqueueRequest) -> QueueItem:
        # like the HTTP client: refuse before any (simulated) network call
        if req.target_model == "":
            raise ValueError("target_model is empty")
        if (req.target_model is None) == (req.printer_id is None):
            raise ValueError("exactly one of target_model and printer_id is needed")
        if req.insert_position is not None and req.insert_position < 1:
            raise ValueError("insert_position starts at 1")
        failure = await self._write_call("enqueue")
        # Bambuddy's order (`add_to_queue`): the printer, then the model with an active printer,
        # then the library file, then what the file says about itself.
        printer = None
        target_model = req.target_model
        if req.printer_id is not None:
            printer = next((p for p in self._printers if p.id == req.printer_id), None)
            if printer is None:
                raise BambuddyRejected("Printer not found", status=400)
        else:
            # Matched without regard to case; stored the way the printer spells it. Left out: Bambuddy's
            # `normalize_model_name` also turns internal codes and long names ("C13", "Bambu Lab X1 Carbon")
            # into the short name first; Reprintbuddy only ever sends the short names of its own printers.
            match = next(
                (
                    p for p in self._printers
                    if p.active and p.model.lower() == (req.target_model or "").lower()
                ),
                None,
            )
            if match is None:
                raise BambuddyRejected(
                    f"No active printers for model: {req.target_model}", status=400
                )
            target_model = match.model
        file = self._files.get(req.library_file_id)
        if file is None:
            raise BambuddyRejected("Library file not found", status=404)
        if target_model is not None and not _gcode_compatible(file.printer_model, target_model):
            raise BambuddyRejected(
                f"File was sliced for {file.printer_model} and cannot be dispatched to "
                f"{target_model} printers",
                status=400,
            )
        if not file.sliced and file.filename.lower().endswith(".3mf"):  # Bambuddy: `ensure_sliced` looks at .3mf only
            raise BambuddyRejected(
                f"{file.filename} isn't sliced yet. Slice it first, then print the sliced file.",
                status=400,
            )
        highest = self._highest()
        if req.insert_position is not None:  # wins over insert_at_top, as in Bambuddy
            position = min(req.insert_position, highest + 1)
        elif req.insert_at_top:
            position = 1
        else:
            position = highest + 1
        for other in self._pending():
            if other.position is not None and other.position >= position:
                self._put(dataclasses.replace(other, position=other.position + 1))
        item = self._new_item(
            position=position,
            printer_id=req.printer_id,
            target_model=target_model,
            label=f"{file.filename} / plate {req.plate_id}",
            created_by=self.username,
            library_file_id=req.library_file_id,
            plate_id=req.plate_id,
            printer_name=printer.name if printer else None,
        )
        self.enqueued.append(req)
        self.options[item.id] = req.print_options
        if failure:
            raise failure[0]
        return item

    async def upload_file(self, folder_id: int | None, filename: str, content: BinaryIO) -> UploadedFile:
        """Like Bambuddy (`library.py:2509-2677`): any name, a same name again, kind by content."""
        failure = await self._write_call("upload_file")
        if not self.can_upload:
            raise BambuddyRejected("API key does not have 'can_manage_library' permission", status=403)
        if folder_id is not None:
            folder = next((f for f in self._folders if f.id == folder_id), None)
            if folder is None:
                raise BambuddyRejected("Folder not found", status=404)
            if folder.id in self._readonly:
                raise BambuddyRejected("Cannot upload to a read-only external folder", status=403)
        content.seek(0)
        data = content.read()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                sliced = any(name.endswith(".gcode") for name in archive.namelist())
        except zipfile.BadZipFile:
            sliced = False
        digest = hashlib.sha256(data).hexdigest()
        duplicate_of = next((fid for fid, h in self._hashes.items() if h == digest and fid in self._files), None)
        file = LibraryFile(
            self._next_file, filename, folder_id, digest, "A1" if sliced else None, sliced, sliced,
            created_by=self.username, size=len(data), created_at=self.now(),
        )
        self._next_file += 1
        self._files[file.id] = file
        self._plates[file.id] = 1 if sliced else 0
        self._hashes[file.id] = digest
        self.uploaded.append((folder_id, filename, data))
        if failure:
            raise failure[0]
        return UploadedFile(id=file.id, filename=filename, sliced=sliced, duplicate_of=duplicate_of)

    async def _queue_read(self, name: str, read):
        await asyncio.sleep(self.delay)
        self._enter(name)
        return read()

    async def get_queue_item(self, item_id: int) -> QueueItem | None:
        return await self._queue_read("get_queue_item", lambda: self._items.get(item_id))

    async def list_queue(self) -> list[QueueItem]:
        def open_items() -> list[QueueItem]:
            items = [i for i in self._items.values() if i.status in ("pending", "printing")]
            return sorted(items, key=lambda i: (i.position is None, i.position or 0, i.id))

        return await self._queue_read("list_queue", open_items)

    async def list_all_items(self) -> list[QueueItem]:
        """Every entry, history included, sorted as Bambuddy does: by (position, id)."""

        def every_item() -> list[QueueItem]:
            return sorted(self._items.values(), key=lambda i: (i.position or 0, i.id))

        return await self._queue_read("list_all_items", every_item)
