"""Database tables. Every building block adds its tables here."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """One setting; `value` holds JSON."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class User(Base):
    """The single admin account. Only the password hash is stored."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(String)


class LoginSession(Base):
    """One signed-in browser. The cookie carries a random id; only its hash is stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)  # UTC
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)  # UTC


# ---- B3: catalog, stock, orders, journal ---------------------------------------------


def utcnow() -> datetime:
    """Naive UTC, the way all times are stored."""
    return datetime.now(UTC).replace(tzinfo=None)


def _int(default: int) -> Mapped[int]:
    return mapped_column(default=default, server_default=str(default))


def _flag() -> Mapped[bool]:
    return mapped_column(default=False, server_default="0")


class Article(Base):
    """A stock article, made from one plate of one print file (Spec 5)."""

    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_articles_stock"),
        CheckConstraint("reserved >= 0", name="ck_articles_reserved"),
        CheckConstraint("provisional >= 0", name="ck_articles_provisional"),
        CheckConstraint("awaiting_assembly >= 0", name="ck_articles_awaiting_assembly"),
        CheckConstraint("units_per_print >= 1", name="ck_articles_units_per_print"),
        # A file/plate belongs to one *active* article; archived ones free it (Spec 5.5).
        Index(
            "uq_articles_active_plate",
            "library_file_id",
            "plate_id",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    sku: Mapped[str | None] = mapped_column(String, unique=True, default=None)
    library_file_id: Mapped[int]
    plate_id: Mapped[int]  # Plate.index, starting at 1
    file_name: Mapped[str] = mapped_column(String)
    file_fingerprint: Mapped[str | None] = mapped_column(String, default=None)
    units_per_print: Mapped[int] = _int(1)
    min_stock: Mapped[int] = _int(0)
    buffer: Mapped[int] = _int(0)
    target_model: Mapped[str | None] = mapped_column(String, default=None)
    printer_id: Mapped[int | None] = mapped_column(default=None)
    needs_assembly: Mapped[bool] = _flag()
    auto_enabled: Mapped[bool] = _flag()
    paused: Mapped[bool] = _flag()
    pause_reason: Mapped[str | None] = mapped_column(String, default=None)
    consecutive_failures: Mapped[int] = _int(0)
    stock: Mapped[int] = _int(0)
    reserved: Mapped[int] = _int(0)
    provisional: Mapped[int] = _int(0)
    awaiting_assembly: Mapped[int] = _int(0)
    thumbnail: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )


class ArticleFilament(Base):
    __tablename__ = "article_filaments"
    __table_args__ = (Index("uq_article_filaments_slot", "article_id", "slot", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    slot: Mapped[int]
    grams: Mapped[float] = mapped_column(Float)
    material: Mapped[str | None] = mapped_column(String, default=None)
    color_hex: Mapped[str | None] = mapped_column(String, default=None)
    spool_filament_id: Mapped[int | None] = mapped_column(default=None)  # B6


class Part(Base):
    """A purchased part that is built into articles (Spec 5.3)."""

    __tablename__ = "parts"
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_parts_stock"),
        CheckConstraint("pack_size >= 1", name="ck_parts_pack_size"),
        CheckConstraint("min_order_packs >= 1", name="ck_parts_min_order_packs"),
        CheckConstraint("safety_unit IN ('days', 'qty')", name="ck_parts_safety_unit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    unit: Mapped[str] = mapped_column(String, default="pcs", server_default="pcs")
    stock: Mapped[int] = _int(0)
    min_stock: Mapped[int] = _int(0)
    pack_size: Mapped[int] = _int(1)
    min_order_packs: Mapped[int] = _int(1)
    lead_time_days: Mapped[int] = _int(0)
    safety_value: Mapped[int] = _int(0)
    safety_unit: Mapped[str] = mapped_column(String, default="days", server_default="days")
    supplier: Mapped[str | None] = mapped_column(String, default=None)
    order_url: Mapped[str | None] = mapped_column(String, default=None)


class ArticlePart(Base):
    __tablename__ = "article_parts"
    __table_args__ = (CheckConstraint("qty_per_unit >= 1", name="ck_article_parts_qty"),)

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), primary_key=True)
    qty_per_unit: Mapped[int]


class ProductSet(Base):
    """A set has no stock of its own (Spec 5.4)."""

    __tablename__ = "product_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    sku: Mapped[str | None] = mapped_column(String, unique=True, default=None)


class SetPosition(Base):
    __tablename__ = "set_positions"
    __table_args__ = (CheckConstraint("qty >= 1", name="ck_set_positions_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("product_sets.id"))
    sort: Mapped[int] = _int(0)
    qty: Mapped[int]


class SetPositionOption(Base):
    __tablename__ = "set_position_options"

    position_id: Mapped[int] = mapped_column(ForeignKey("set_positions.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'shipped', 'cancelled')", name="ck_orders_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String)
    order_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="open", server_default="open")
    note: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (CheckConstraint("qty >= 1", name="ck_order_lines_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    group_no: Mapped[int] = _int(1)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    qty: Mapped[int]
    set_id: Mapped[int | None] = mapped_column(ForeignKey("product_sets.id"), default=None)
    set_qty: Mapped[int | None] = mapped_column(default=None)


class JournalEntry(Base):
    """Every booking and every action of the automation (Spec 6.3). Never changed or deleted."""

    __tablename__ = "journal"
    __table_args__ = (
        CheckConstraint("actor IN ('user', 'auto')", name="ck_journal_actor"),
        Index("ix_journal_article_id", "article_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)
    actor: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id"), default=None)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None)
    qty: Mapped[int] = _int(0)
    note: Mapped[str | None] = mapped_column(String, default=None)
    details: Mapped[dict | None] = mapped_column(JSON, default=None)


class FormToken(Base):
    """A used one-time form token (see web/formtoken.py)."""

    __tablename__ = "form_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---- B4: work queue, dispatches, inspections -----------------------------------------


class WorkQueueEntry(Base):
    """Prints that are wanted but not yet handed to Bambuddy (Spec 10)."""

    __tablename__ = "work_queue"
    __table_args__ = (
        CheckConstraint("prints >= 1", name="ck_work_queue_prints"),
        CheckConstraint(
            "origin IN ('rule', 'scrap', 'failure', 'manual')", name="ck_work_queue_origin"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    prints: Mapped[int]
    origin: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(index=True)
    held: Mapped[bool] = _flag()
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )


class Dispatch(Base):
    """One print handed to Bambuddy, followed until it is finished (Spec 10)."""

    __tablename__ = "dispatches"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('rule', 'scrap', 'failure', 'manual')", name="ck_dispatches_origin"
        ),
        CheckConstraint(
            "status IN ('sending', 'unclear', 'queued', 'printing', 'completed', 'failed',"
            " 'cancelled', 'missing', 'returned')",
            name="ck_dispatches_status",
        ),
        CheckConstraint("units >= 1", name="ck_dispatches_units"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    origin: Mapped[str] = mapped_column(String)
    # Unused since v0.3.5 (Spec 7.6: single entries, no batches). Kept without a migration:
    # `external_ref` is NOT NULL UNIQUE, so each new print still gets a fresh uuid; older
    # rows keep their batch id. Nothing reads either column any more.
    external_ref: Mapped[str] = mapped_column(String, unique=True)
    bambuddy_batch_id: Mapped[int | None] = mapped_column(default=None)
    bambuddy_item_id: Mapped[int | None] = mapped_column(index=True, default=None)
    status: Mapped[str] = mapped_column(String, index=True)
    units: Mapped[int]  # pieces per print, as reserved
    enqueue_sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    queue_entry_id: Mapped[int | None] = mapped_column(default=None)  # no FK: the entry may be gone
    queue_position: Mapped[int]
    last_status: Mapped[str | None] = mapped_column(String, default=None)  # Bambuddy's raw state
    message: Mapped[str | None] = mapped_column(String, default=None)  # why it was put back
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Inspection(Base):
    """A finished print waiting for the count of good pieces (Spec 10)."""

    __tablename__ = "inspections"
    __table_args__ = (
        CheckConstraint("units >= 1", name="ck_inspections_units"),
        CheckConstraint(
            "good_units IS NULL OR good_units BETWEEN 0 AND units", name="ck_inspections_good_units"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey("dispatches.id"), unique=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    units: Mapped[int]
    good_units: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


# ---- B6: filament stock and reorder suggestions --------------------------------------


class FilamentSort(Base):
    """A kind of filament as Bambuddy's spools show it; `id` is what `spool_filament_id` means."""

    __tablename__ = "filament_sorts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sort_key: Mapped[str] = mapped_column(String, unique=True)
    brand: Mapped[str | None] = mapped_column(String, default=None)
    material: Mapped[str] = mapped_column(String)
    subtype: Mapped[str | None] = mapped_column(String, default=None)
    color_hex: Mapped[str | None] = mapped_column(String, default=None)
    color_name: Mapped[str | None] = mapped_column(String, default=None)
    label: Mapped[str] = mapped_column(String)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )


class FilamentSettings(Base):
    __tablename__ = "filament_settings"
    __table_args__ = (
        CheckConstraint("safety_unit IN ('days', 'grams')", name="ck_filament_settings_unit"),
        CheckConstraint("pack_grams >= 1", name="ck_filament_settings_pack_grams"),
        CheckConstraint("min_order_packs >= 1", name="ck_filament_settings_min_order_packs"),
    )

    spool_filament_id: Mapped[int] = mapped_column(ForeignKey("filament_sorts.id"), primary_key=True)
    lead_time_days: Mapped[int] = _int(0)
    safety_value: Mapped[int] = _int(0)
    safety_unit: Mapped[str] = mapped_column(String, default="days", server_default="days")
    pack_grams: Mapped[int] = _int(1000)
    min_order_packs: Mapped[int] = _int(1)
    supplier: Mapped[str | None] = mapped_column(String, default=None)
    order_url: Mapped[str | None] = mapped_column(String, default=None)


class FilamentSnapshot(Base):
    """One row per UTC day and spool: what Bambuddy reported (the source of the usage)."""

    __tablename__ = "filament_snapshots"
    __table_args__ = (
        Index("uq_filament_snapshots_day_spool", "day", "spool_id", unique=True),
        Index("ix_filament_snapshots_sort_day", "spool_filament_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    spool_id: Mapped[int]
    spool_filament_id: Mapped[int] = mapped_column(ForeignKey("filament_sorts.id"))
    remaining_g: Mapped[float] = mapped_column(Float)
    label_g: Mapped[int]
    spool_created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ReorderMark(Base):
    """'Ordered': a suggestion the user has acted on; `qty` counts order units. Not a booking."""

    __tablename__ = "reorder_marks"
    __table_args__ = (
        CheckConstraint("item_type IN ('filament', 'part')", name="ck_reorder_marks_item_type"),
        CheckConstraint("qty >= 1", name="ck_reorder_marks_qty"),
        Index("ix_reorder_marks_item", "item_type", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_type: Mapped[str] = mapped_column(String)
    item_id: Mapped[int]  # filament_sorts.id or parts.id, by item_type
    qty: Mapped[int]
    marked_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
