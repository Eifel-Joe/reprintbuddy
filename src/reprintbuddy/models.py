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
