"""Data classes of Bambuddy's answers. B2 completes `QueueItem` and `Spool`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LibraryFolder:
    id: int
    name: str
    parent_id: int | None


@dataclass(frozen=True)
class LibraryFile:
    id: int
    filename: str
    folder_id: int | None
    fingerprint: str | None  # changes with every new version of the file (B2 defines the source)
    printer_model: str | None  # the model the file was sliced for


@dataclass(frozen=True)
class Plate:
    index: int  # starts at 1
    name: str | None


@dataclass(frozen=True)
class FilamentRequirement:
    slot: int
    grams: float
    material: str | None
    color_hex: str | None


@dataclass(frozen=True)
class EnqueueRequest:
    library_file_id: int
    plate_id: int
    target_model: str | None
    printer_id: int | None
    batch_id: int
    insert_at_top: bool
    insert_position: int | None


@dataclass(frozen=True)
class Printer:
    id: int
    name: str
    model: str
    active: bool = True  # False while the printer is in maintenance mode in Bambuddy


# The states of a queue entry as Bambuddy names them (PrintQueueItemResponse.status).
# `QueueItem.status` keeps Bambuddy's value as it came, also one not listed here:
# what an unknown state means is decided in one place by the queue logic (B4).
QUEUE_STATUSES: tuple[str, ...] = ("pending", "printing", "completed", "failed", "skipped", "cancelled")


@dataclass(frozen=True)
class QueueItem:
    """One entry of Bambuddy's print queue (`GET /queue/{id}`, `GET /queue/`, `POST /queue/`)."""

    id: int
    status: str  # raw, see QUEUE_STATUSES
    batch_id: int | None
    position: int | None
    printer_id: int | None  # set once Bambuddy assigned a printer
    target_model: str | None  # "Any <model>" entries
    label: str  # file name or archive name, "#<id>" without either; " / plate <n>" with a plate
    created_by: str | None  # Bambuddy user name of the creator
    library_file_id: int | None
    plate_id: int | None
    printer_name: str | None
    manual_start: bool  # True: waits until someone starts it in Bambuddy
    waiting_reason: str | None  # Bambuddy's text why a pending entry has not started
    started_at: datetime | None  # naive UTC
    completed_at: datetime | None  # naive UTC
    error_message: str | None


@dataclass(frozen=True)
class Spool:
    """Fields follow plan assumption S1 (B6); B2's version replaces this on merge."""

    id: int
    material: str
    subtype: str | None
    brand: str | None
    color_name: str | None
    color_hex: str | None  # "#RRGGBB"
    label_g: int
    remaining_g: float
    archived: bool
    created_at: datetime | None
