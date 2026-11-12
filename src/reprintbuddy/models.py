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
    UniqueConstraint,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, configure_mappers, mapped_column


class Base(DeclarativeBase):
    """Shared base of all tables.

    Assigning a public name that is neither a mapped attribute (column, relationship, ...) nor a
    settable descriptor of the class raises `AttributeError`. Without this guard SQLAlchemy keeps
    such a value as a plain Python attribute and never stores it, so code or a test that still
    writes a column moved elsewhere (e.g. `Part.pack_size`, moved to `PartSupplier` in 0015)
    passed silently. Private names (`_...`, including SQLAlchemy's `_sa_*`) stay free. A table
    that wants a deliberate extra attribute lists it in `__extra_attrs__`.
    """

    __extra_attrs__: frozenset[str] = frozenset()

    def __setattr__(self, name: str, value: object) -> None:
        if not name.startswith("_") and name not in _settable(type(self)):
            raise AttributeError(
                f"{type(self).__name__} has no field {name!r}; assigning it would not be stored"
            )
        super().__setattr__(name, value)


_SETTABLE: dict[type, frozenset[str]] = {}


def _settable(cls: type) -> frozenset[str]:
    """Public names an instance of `cls` may be assigned; computed once per class."""
    names = _SETTABLE.get(cls)
    if names is None:
        mapper = sa_inspect(cls)
        if not mapper.configured:
            # Review C 7: a backref appears on its target only when the mappers are configured; a
            # list made before that would refuse it for good.
            configure_mappers()
        mapped = set(mapper.all_orm_descriptors.keys())
        descriptors = {
            name for klass in cls.__mro__ for name, attr in vars(klass).items()
            if hasattr(attr, "__set__")
        }
        names = frozenset(mapped | descriptors | set(cls.__extra_attrs__))
        _SETTABLE[cls] = names
    return names


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
    """A stock article (Spec 5). Since 0.4.0 (p341, Spec 5.6) one of three kinds:
    `print` made from one plate of one print file, `assembled` made of parts (a bill of materials in
    `article_components`, built on stock or on order), and `variant`, one variant of an assembled
    article with choices: its own row under `parent_id`, keyed by its options, so stock, journal,
    counts and order lines work for it as for any article."""

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
        CheckConstraint("kind IN ('print', 'assembled', 'variant')", name="ck_articles_kind"),
        CheckConstraint(
            "kind <> 'print' OR (library_file_id IS NOT NULL AND plate_id IS NOT NULL AND file_name IS NOT NULL)",
            name="ck_articles_print_has_plate",
        ),
        CheckConstraint(
            "(kind = 'variant') = (parent_id IS NOT NULL AND variant_key IS NOT NULL)",
            name="ck_articles_variant_parent",
        ),
        # COALESCE: a CHECK that comes out NULL passes, so a missing mode must compare as false.
        CheckConstraint(
            "(kind = 'assembled') = (COALESCE(assembly_mode, '') IN ('stock', 'order'))",
            name="ck_articles_assembly_mode",
        ),
        # One active row per variant of an assembled article; an archived one keeps its number (Spec 5.6).
        Index(
            "uq_articles_variant_key",
            "parent_id",
            "variant_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    sku: Mapped[str | None] = mapped_column(String, unique=True, default=None)
    label_text: Mapped[str | None] = mapped_column(String, default=None)  # free text for the label (Spec 5.2)
    # Only a print article has a plate (ck_articles_print_has_plate); NULL for assembled and variant rows.
    library_file_id: Mapped[int | None]
    plate_id: Mapped[int | None]  # Plate.index, starting at 1
    file_name: Mapped[str | None] = mapped_column(String)
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
    kind: Mapped[str] = mapped_column(String, default="print", server_default="print")
    # "einzeln erhaeltlich" (Spec 5.2): may be ordered and booked on its own; assembled always are.
    sold_alone: Mapped[bool] = mapped_column(default=True, server_default="1")
    assembly_mode: Mapped[str | None] = mapped_column(String, default=None)  # 'stock' | 'order' (Spec 5.6)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)
    variant_key: Mapped[str | None] = mapped_column(String, default=None)  # "<option1_id>[:<option2_id>]"


class ArticleOption(Base):
    """One option of a choice field of an assembled article (Spec 5.6): its field (1 or 2), its place
    in the field and its suffix, which makes the variant number. Its articles are in
    `article_components` with this `option_id`."""

    __tablename__ = "article_options"
    __table_args__ = (
        CheckConstraint("field IN (1, 2)", name="ck_article_options_field"),
        CheckConstraint("suffix <> ''", name="ck_article_options_suffix"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    field: Mapped[int]
    sort: Mapped[int] = _int(0)
    suffix: Mapped[str] = mapped_column(String)


class ArticleComponent(Base):
    """One row of the bill of materials of an assembled article (Spec 5.6): a print article and its
    quantity, either a base part (`field` 0) or one article of an option (`field` 1 or 2)."""

    __tablename__ = "article_components"
    __table_args__ = (
        CheckConstraint("qty >= 1", name="ck_article_components_qty"),
        CheckConstraint("field IN (0, 1, 2)", name="ck_article_components_field"),
        CheckConstraint("(field = 0) = (option_id IS NULL)", name="ck_article_components_option"),
        Index("ix_article_components_part", "part_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    field: Mapped[int] = _int(0)
    option_id: Mapped[int | None] = mapped_column(ForeignKey("article_options.id"), default=None)
    part_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    qty: Mapped[int]
    sort: Mapped[int] = _int(0)


class ArticleFilament(Base):
    __tablename__ = "article_filaments"
    __table_args__ = (Index("uq_article_filaments_slot", "article_id", "slot", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    slot: Mapped[int]
    grams: Mapped[float] = mapped_column(Float)
    material: Mapped[str | None] = mapped_column(String, default=None)
    color_hex: Mapped[str | None] = mapped_column(String, default=None)


class ArticleFilamentSort(Base):
    """A spool sort ticked for one slot of an article (the filament group, Spec 5.2, 9.1); at most one
    per slot `reordered`. Keyed by article and slot, because a new print file replaces the slot rows."""

    __tablename__ = "article_filament_sorts"
    __table_args__ = (
        Index(
            "uq_article_filament_sorts_reordered", "article_id", "slot", unique=True,
            sqlite_where=text("reordered = 1"),
        ),
        Index("ix_article_filament_sorts_sort", "sort_id"),
    )

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    slot: Mapped[int] = mapped_column(primary_key=True)
    sort_id: Mapped[int] = mapped_column(ForeignKey("filament_sorts.id"), primary_key=True)
    reordered: Mapped[bool] = _flag()


class Part(Base):
    """A purchased part that is built into articles (Spec 5.3); where it is bought: `PartSupplier`."""

    __tablename__ = "parts"
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_parts_stock"),
        CheckConstraint("safety_unit IN ('days', 'qty')", name="ck_parts_safety_unit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    unit: Mapped[str] = mapped_column(String, default="pcs", server_default="pcs")
    stock: Mapped[int] = _int(0)
    min_stock: Mapped[int] = _int(0)
    safety_value: Mapped[int] = _int(0)
    safety_unit: Mapped[str] = mapped_column(String, default="days", server_default="days")


class PartSupplier(Base):
    """One supplier of a purchased part (Spec 5.3): 1 to 10 per part, exactly one the default.

    `position` is the order in the form (0 first); the name may be empty ("Supplier n" is shown).
    """

    __tablename__ = "part_suppliers"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_part_suppliers_position"),
        CheckConstraint("pack_size >= 1", name="ck_part_suppliers_pack_size"),
        CheckConstraint("min_order_packs >= 1", name="ck_part_suppliers_min_order_packs"),
        CheckConstraint("lead_time_days >= 0", name="ck_part_suppliers_lead_time_days"),
        UniqueConstraint("part_id", "position", name="uq_part_suppliers_position"),
        Index(
            "uq_part_suppliers_default", "part_id", unique=True, sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    position: Mapped[int]
    name: Mapped[str | None] = mapped_column(String, default=None)
    order_url: Mapped[str | None] = mapped_column(String, default=None)
    pack_size: Mapped[int] = _int(1)
    min_order_packs: Mapped[int] = _int(1)
    lead_time_days: Mapped[int] = _int(0)
    is_default: Mapped[bool] = _flag()


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
    """A base part (`slot` 0, one option) or a choice field (`slot` 1 or 2, one position per set
    holding all its options); the quantity counts for the article that is chosen (Spec 5.4, p340)."""

    __tablename__ = "set_positions"
    __table_args__ = (
        CheckConstraint("qty >= 1", name="ck_set_positions_qty"),
        CheckConstraint("slot IN (0, 1, 2)", name="ck_set_positions_slot"),
        Index("uq_set_positions_slot", "set_id", "slot", unique=True, sqlite_where=text("slot > 0")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("product_sets.id"))
    sort: Mapped[int] = _int(0)
    qty: Mapped[int]
    slot: Mapped[int] = _int(0)


class SetPositionOption(Base):
    """An article of a position; in a choice field with its suffix (part of the variant number) and
    its place in the field."""

    __tablename__ = "set_position_options"

    position_id: Mapped[int] = mapped_column(ForeignKey("set_positions.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    suffix: Mapped[str | None] = mapped_column(String, default=None)
    sort: Mapped[int] = _int(0)


class SetPositionSet(Base):
    """An inner set of a base row (Spec 5.4, 0.4.3 p423): the set and its fixed variant, the chosen
    article per choice field of the inner set (empty without choice parts). A base row holds either
    this or one SetPositionOption with an article."""

    __tablename__ = "set_position_sets"

    position_id: Mapped[int] = mapped_column(ForeignKey("set_positions.id"), primary_key=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("product_sets.id"))
    choice1_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)
    choice2_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(a for a in (self.choice1_id, self.choice2_id) if a is not None)


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
    variant_sku: Mapped[str | None] = mapped_column(String, default=None)  # as ordered (Spec 6.7, p340)
    # The assembled article (built on order) this group of part lines stands for (Spec 6.7, 0.4.0 p341).
    assembled_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)
    # The way through the inner sets of the ordered set, as ordered (Spec 6.7, 0.4.3 p423):
    # JSON [[set_id, variant_sku, units], ...] from the outside in; None directly in the ordered set.
    inner_path: Mapped[str | None] = mapped_column(String, default=None)


def _booked_default(context) -> datetime:
    """A journal row without a booking date is booked when it is saved: its `at`."""
    return context.get_current_parameters()["at"]


class JournalEntry(Base):
    """Every booking and every action of the automation (Spec 6.3). Never changed or deleted."""

    __tablename__ = "journal"
    __table_args__ = (
        CheckConstraint("actor IN ('user', 'auto')", name="ck_journal_actor"),
        Index("ix_journal_article_id", "article_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)  # when it was saved
    # When the booking counts (Spec 6.2, p172): a hand booking may be dated back; else the same as `at`.
    booked_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=_booked_default)
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
            " 'cancelled', 'missing', 'returned', 'withdrawn')",
            name="ck_dispatches_status",
        ),
        CheckConstraint("units >= 1", name="ck_dispatches_units"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    origin: Mapped[str] = mapped_column(String)
    bambuddy_item_id: Mapped[int | None] = mapped_column(index=True, default=None)
    status: Mapped[str] = mapped_column(String, index=True)
    units: Mapped[int]  # pieces per print, as reserved
    enqueue_sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    queue_entry_id: Mapped[int | None] = mapped_column(default=None)  # no FK: the entry may be gone
    queue_position: Mapped[int]
    # The file and plate this print is handed over with, taken from the article at the reservation
    # (Spec 7.6, v0.3.11 p53): the search after an unclear answer looks for these, so changing the
    # article's file meanwhile does not hide the entry. Empty for prints from before revision 0010;
    # the search then uses the article's file, as before.
    library_file_id: Mapped[int | None] = mapped_column(default=None)
    plate_id: Mapped[int | None] = mapped_column(default=None)
    last_status: Mapped[str | None] = mapped_column(String, default=None)  # Bambuddy's raw state
    message: Mapped[str | None] = mapped_column(String, default=None)  # why it was put back
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # Set while Reprintbuddy's delete of this print in Bambuddy is under way (Spec 7.2a, 0.4.1 p382):
    # a print that then is gone was withdrawn, not deleted by somebody else (no pause, Spec 7.6).
    withdraw_sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Withdrawal(Base):
    """Prints of one article still to be withdrawn from Bambuddy after a cancel (Spec 7.2a, p382).

    `prints` is what the cancel found over the target; each run deletes at most what is still over
    it then. `refused` keeps Bambuddy's answer when it refused (no further try); `unreached` says a
    run could not reach Bambuddy (the overview says so until the withdrawal is done or gone).
    """

    __tablename__ = "withdrawals"
    __table_args__ = (CheckConstraint("prints >= 0", name="ck_withdrawals_prints"),)

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    prints: Mapped[int] = _int(0)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None)
    refused: Mapped[str | None] = mapped_column(String, default=None)
    unreached: Mapped[bool] = _flag()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )


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
    """A spool sort: brand, material, subtype and colour value of Bambuddy's spools (Spec 4, 9.1);
    `id` is what `spool_filament_id` means. `legacy`: a brandless sort of 0006, waiting for
    `filament.convert_legacy`; it takes no spools and shows nowhere."""

    __tablename__ = "filament_sorts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sort_key: Mapped[str] = mapped_column(String, unique=True)
    legacy: Mapped[bool] = _flag()
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
    """The order settings of a spool sort (Spec 9.3), optional: the safety margin here, the rest per
    supplier (`FilamentSupplier`, since 0016). Saved once, a sort has at least one supplier."""

    __tablename__ = "filament_settings"
    __table_args__ = (
        CheckConstraint("safety_unit IN ('days', 'grams')", name="ck_filament_settings_unit"),
    )

    spool_filament_id: Mapped[int] = mapped_column(ForeignKey("filament_sorts.id"), primary_key=True)
    safety_value: Mapped[int] = _int(0)
    safety_unit: Mapped[str] = mapped_column(String, default="days", server_default="days")


class FilamentSupplier(Base):
    """One supplier of a spool sort (Spec 9.3): 1 to 10 per sort with settings, exactly one the
    default. The same columns as `PartSupplier`; `pack_size` is the grams of one roll. It only
    serves the reorder suggestion: filament is never booked in Reprintbuddy (Spec 9.1)."""

    __tablename__ = "filament_suppliers"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_filament_suppliers_position"),
        CheckConstraint("pack_size >= 1", name="ck_filament_suppliers_pack_size"),
        CheckConstraint("min_order_packs >= 1", name="ck_filament_suppliers_min_order_packs"),
        CheckConstraint("lead_time_days >= 0", name="ck_filament_suppliers_lead_time_days"),
        UniqueConstraint("sort_id", "position", name="uq_filament_suppliers_position"),
        Index(
            "uq_filament_suppliers_default", "sort_id", unique=True, sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sort_id: Mapped[int] = mapped_column(ForeignKey("filament_settings.spool_filament_id"))
    position: Mapped[int]
    name: Mapped[str | None] = mapped_column(String, default=None)
    order_url: Mapped[str | None] = mapped_column(String, default=None)
    pack_size: Mapped[int] = _int(1000)
    min_order_packs: Mapped[int] = _int(1)
    lead_time_days: Mapped[int] = _int(0)
    is_default: Mapped[bool] = _flag()


class FilamentSnapshot(Base):
    """One row per UTC day and spool: what Bambuddy reported (the source of the usage)."""

    __tablename__ = "filament_snapshots"
    __table_args__ = (
        Index("uq_filament_snapshots_day_spool", "day", "spool_id", unique=True),
        Index("ix_filament_snapshots_sort_day", "spool_filament_id", "day"),
        Index("ix_filament_snapshots_spool", "spool_id"),  # 0007: rows follow their spool
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
    # The pack size and the supplier name when it was marked (Spec 9.4: the amount on its way stays
    # as ordered, whatever happens to the supplier later); parts since 0015, filament since 0016.
    pack_size: Mapped[int | None] = mapped_column(default=None)
    supplier_name: Mapped[str | None] = mapped_column(String, default=None)
    supplier_position: Mapped[int | None] = mapped_column(default=None)  # "Supplier n" is position + 1
    # Closed by "take back" (Spec 9.4, p280), not by arrival: `received_at` is then the time it was
    # taken back. Every "is it open" stays `received_at IS NULL`; only what means "arrived" (push
    # messages settling, the activity log) looks at this flag.
    withdrawn: Mapped[bool] = _flag()


class NotifyMark(Base):
    """What a push message has reported (Spec 10.6 "Keine Flut"), one row per occasion and thing.

    `kind` is the event (`settings.HA_EVENTS`), `ref` the print, article, spool sort, part or warning.
    `state`: `watch` seen but not due yet (a trouble needs two regular queries), `due` to be sent,
    `sent` reported. A state that has settled loses its row, so it reports again when it comes back.
    `streak` counts the regular queries in a row that saw a trouble; `gone_since` is when a reorder
    suggestion was last missing while the stock was known (it settles after 7 days).
    """

    __tablename__ = "notify_marks"
    __table_args__ = (
        CheckConstraint("state IN ('watch', 'due', 'sent')", name="ck_notify_marks_state"),
        CheckConstraint("streak >= 0", name="ck_notify_marks_streak"),
        UniqueConstraint("kind", "ref", name="uq_notify_marks_kind_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    ref: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    streak: Mapped[int] = mapped_column(default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    gone_since: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ActivityEntry(Base):
    """One line of the activity log under Settings (Spec 10.7): what happened outside the journal.

    `area` is the group of the page (login, settings, master data, …), `kind` the event, `actor` who
    (user, automation, Bambuddy, system), `via` "cli" for a command on the server, `address` the
    device of a sign-in event. `details` holds ids, field keys, fixed codes and cleaned values only,
    never a secret (Spec 11.5a); the page builds its text from the language files.
    """

    __tablename__ = "activity"
    __table_args__ = (
        CheckConstraint("actor IN ('user', 'auto', 'bambuddy', 'system')", name="ck_activity_actor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)
    area: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    via: Mapped[str | None] = mapped_column(String, default=None)
    address: Mapped[str | None] = mapped_column(String, default=None)
    details: Mapped[dict | None] = mapped_column(JSON, default=None)


class ActivityMark(Base):
    """The last logged state of something the activity log records only as a change (Spec 10.7):
    the connection to Bambuddy, its rights, failing push messages, the newest version, the result
    of the last update. In the database, so a restart logs no new change."""

    __tablename__ = "activity_marks"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
