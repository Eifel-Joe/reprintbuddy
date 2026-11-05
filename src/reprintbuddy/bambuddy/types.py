"""Data classes of Bambuddy's answers. B2 completes `QueueItem` and `Spool`."""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class QueueItem:
    """Placeholder; B2 adds the fields after the experiment B0."""

    id: int
    status: str


@dataclass(frozen=True)
class Spool:
    """Placeholder; B2 adds the fields after the experiment B0."""

    id: int
