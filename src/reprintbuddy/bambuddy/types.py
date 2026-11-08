"""Data classes of Bambuddy's answers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class LibraryFolder:
    id: int
    name: str
    parent_id: int | None
    # A folder Bambuddy reads from a mounted path (`is_external`): its files have no fingerprint
    # after a rescan, so Reprintbuddy neither takes nor uploads files there (Spec 5.1a, README).
    is_external: bool = False


@dataclass(frozen=True)
class LibraryFile:
    id: int
    filename: str
    folder_id: int | None
    fingerprint: str | None  # changes with every new version of the file (B2 defines the source)
    printer_model: str | None  # the model the file was sliced for
    # Both flags come last and have defaults, so every earlier construction stays valid.
    has_thumbnail: bool = False  # the file list carries a `thumbnail_path`
    sliced: bool = True  # a sliced file (`.gcode.3mf`), the only kind that can become an article
    # Who made the file and when, and its size: to find an upload again after an unclear
    # answer (Spec 5.1a). From the list and the single file alike; `None` when missing.
    created_by: str | None = None
    size: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class UploadedFile:
    """Bambuddy's answer to an upload (`FileUploadResponse`, `schemas/library.py:328-337`)."""

    id: int
    filename: str
    sliced: bool  # Bambuddy's own verdict from the zip (`file_type == "gcode.3mf"`)
    duplicate_of: int | None  # a file with the same content that was already there


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
    """One print as one single queue entry (Spec 7.6): no batch, no quantity."""

    library_file_id: int
    plate_id: int
    target_model: str | None
    printer_id: int | None
    insert_at_top: bool
    insert_position: int | None


@dataclass(frozen=True)
class Printer:
    id: int
    name: str
    model: str
    # False: Bambuddy's maintenance mode (`is_active: false`). Such a printer is only
    # temporarily unavailable; it stays a known print target but is not offered anew.
    active: bool = True


# The states of a queue entry as Bambuddy names them (PrintQueueItemResponse.status).
# `QueueItem.status` keeps Bambuddy's value as it came, also one not listed here:
# what an unknown state means is decided in one place by the queue logic (B4).
QUEUE_STATUSES: tuple[str, ...] = ("pending", "printing", "completed", "failed", "skipped", "cancelled")


@dataclass(frozen=True)
class QueueItem:
    """One entry of Bambuddy's print queue (`GET /queue/{id}`, `GET /queue/`, `POST /queue/`)."""

    id: int
    status: str  # raw, see QUEUE_STATUSES
    batch_id: int | None  # as Bambuddy reports it; Reprintbuddy makes no batches (Spec 7.6)
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
    # Last, with a default: every earlier construction stays valid. Used to find an entry
    # after an unclear hand-over (Spec 7.6).
    created_at: datetime | None = None  # naive UTC


# Which spool management Bambuddy runs: its own ("builtin") or Spoolman ("spoolman").
SpoolMode = Literal["builtin", "spoolman"]


@dataclass(frozen=True)
class Spool:
    """One spool of the running inventory mode; the same fields for both lists (Decision 12)."""

    id: int
    material: str
    subtype: str | None
    brand: str | None
    color_name: str | None
    color_hex: str | None  # `#RRGGBB` from `rgba`; `#RRGGBBAA` when the alpha is not opaque (Spec 9.1)
    label_g: int  # nominal filament weight of a full spool (`label_weight`)
    remaining_g: float  # `label_weight - weight_used`, never below 0
    archived: bool  # `archived_at` is set
    created_at: datetime | None  # naive UTC
