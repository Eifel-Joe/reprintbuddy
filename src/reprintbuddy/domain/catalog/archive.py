"""Archiving and restoring articles, parts and sets instead of deleting them (Spec 5.5)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.activity.capture import quiet
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate
from reprintbuddy.domain import articlesets
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import Actor, write_journal
from reprintbuddy.domain.workqueue import OPEN_STATES
from reprintbuddy.models import (
    Article,
    ArticleComponent,
    ArticleFilament,
    ArticleFilamentSort,
    ArticlePart,
    Dispatch,
    Order,
    OrderLine,
    Part,
    ProductSet,
    WorkQueueEntry,
    utcnow,
)
from reprintbuddy.domain.catalog.articles import _delete_rows
from reprintbuddy.domain.catalog.sets import get_set, sets_holding_article, sets_holding_set


# ---- archive instead of delete (Spec 5.5) ---------------------------------------------------


def _must_be_active(thing) -> None:
    """Archiving something already in the archive is refused (another tab got there first)."""
    if thing.archived_at is not None:
        raise ConflictError("catalog.already_archived")


def _must_be_archived(thing) -> None:
    """Restoring something that is not in the archive is refused."""
    if thing.archived_at is None:
        raise ConflictError("catalog.not_archived")


def _article_or_404(session: Session, article_id: int) -> Article:
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    return article


def busy_reasons(session: Session, article: Article) -> list[str]:
    """What still runs for an article, as error keys in the order of the checks (Spec 5.5): open
    orders, provisional, awaiting assembly, queue, in progress. Shared by archiving and by the switch
    between print article and article made of parts (Spec 5.6, 0.4.0). For an article made of parts
    the open orders of its variant rows and of its groups (built on order) count too."""
    ids = [article.id]
    if article.kind == articlesets.ASSEMBLED:
        ids += list(session.scalars(select(Article.id).where(Article.parent_id == article.id)))
    open_lines = select(OrderLine.id).join(Order, Order.id == OrderLine.order_id).where(Order.status == "open")
    reasons = []
    if (
        session.scalar(open_lines.where(OrderLine.article_id.in_(ids)).limit(1)) is not None
        or session.scalar(open_lines.where(OrderLine.assembled_id == article.id).limit(1)) is not None
    ):
        reasons.append("articles.archive_open_orders")
    if article.provisional > 0:
        reasons.append("articles.archive_provisional")
    if article.awaiting_assembly > 0:
        reasons.append("articles.archive_awaiting_assembly")
    if session.scalar(select(WorkQueueEntry.id).where(WorkQueueEntry.article_id == article.id).limit(1)):
        reasons.append("articles.archive_in_queue")
    if session.scalar(
        select(Dispatch.id).where(Dispatch.article_id == article.id, Dispatch.status.in_(OPEN_STATES)).limit(1)
    ):
        reasons.append("articles.archive_in_progress")
    return reasons


def _assembled_users(session: Session, article_id: int) -> list[str]:
    """Names of the active articles made of parts a print article is a part of (Spec 5.5, 0.4.0)."""
    users = session.scalars(
        select(Article.name)
        .where(
            articlesets.where_assembled(),
            Article.id.in_(select(ArticleComponent.article_id).where(ArticleComponent.part_id == article_id)),
        )
        .order_by(Article.name, Article.id)
    ).all()
    return list(users)


def archive_article(session: Session, article_id: int) -> None:
    """Only when nothing is running (Spec 5.5). A print article that is a part of an active article
    made of parts stays; an article made of parts takes its variant rows into the archive with the
    same time (condition 2 of the plan's approval, 0.4.0), so every `archived_at IS NULL` filter
    leaves them out without a join on the parent."""
    article = _article_or_404(session, article_id)
    if article.kind == articlesets.VARIANT:
        raise NotFoundError("articles.not_found")  # a variant goes with its article made of parts
    _must_be_active(article)
    users = _assembled_users(session, article.id) if article.kind == articlesets.PRINT else []
    if users:
        raise ConflictError("articles.archive_in_assembled", names=", ".join(users))
    sets = sets_holding_article(session, article.id)
    if sets:  # Spec 5.4 "Archivieren und Sets", 5.5 (0.4.3): every active set stays orderable
        raise ConflictError("articles.archive_in_set", names=", ".join(sets))
    reasons = busy_reasons(session, article)
    if reasons:
        raise ConflictError(reasons[0])
    now = utcnow()
    article.archived_at = now
    if article.kind == articlesets.ASSEMBLED:
        for row in session.scalars(articlesets.variant_rows(article.id)).all():
            row.archived_at = now
    session.flush()


def _plate_holder(session: Session, file_id: int, plate_index: int, *, besides: int) -> Article | None:
    return session.scalar(
        select(Article).where(
            Article.library_file_id == file_id,
            Article.plate_id == plate_index,
            articlesets.where_print(),
            Article.id != besides,
        )
    )


def _check_parts_active(session: Session, article_id: int) -> None:
    archived = session.scalar(
        select(Part)
        .join(ArticlePart, ArticlePart.part_id == Part.id)
        .where(ArticlePart.article_id == article_id, Part.archived_at.is_not(None))
        .limit(1)
    )
    if archived is not None:
        raise ValidationError("parts.archived", name=archived.name)


def restore_article(session: Session, article_id: int) -> Article:
    """Bring an article back. If its plate has been taken meanwhile, the caller offers
    `restore_article_with_plate` with another file."""
    article = _article_or_404(session, article_id)
    if article.kind == articlesets.VARIANT:  # review finding 5: a variant comes back with its article
        raise NotFoundError("articles.not_found")
    _must_be_archived(article)
    if article.kind == articlesets.ASSEMBLED:
        return _restore_assembled(session, article)
    holder = _plate_holder(session, article.library_file_id, article.plate_id, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.restore_plate_taken", name=holder.name)
    _check_parts_active(session, article_id)
    article.archived_at = None
    session.flush()
    return article


def _restore_assembled(session: Session, article: Article) -> Article:
    """An article made of parts comes back with the variant rows of its options (condition 2 of the
    plan's approval); rows archived before because their option went stay archived."""
    from reprintbuddy.domain import assembled  # assembled builds on this module

    archived_part = session.scalar(
        select(Article)
        .where(
            Article.archived_at.is_not(None),
            Article.id.in_(select(ArticleComponent.part_id).where(ArticleComponent.article_id == article.id)),
        )
        .order_by(Article.name)
        .limit(1)
    )
    if archived_part is not None:
        raise ValidationError("assembled.part_archived", name=archived_part.name)
    _check_parts_active(session, article.id)
    article.archived_at = None
    session.flush()
    assembled.sync_variants(session, article)
    return article


def restore_article_with_plate(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    plate: Plate,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
) -> Article:
    """Restore with another file and plate; takes over the file data like `create_article`."""
    article = _article_or_404(session, article_id)
    _must_be_archived(article)
    holder = _plate_holder(session, file.id, plate.index, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.plate_taken", name=holder.name)
    _check_parts_active(session, article_id)
    article.library_file_id = file.id
    article.plate_id = plate.index
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    article.archived_at = None
    session.flush()
    return article


def _filament_kinds(session: Session, article_id: int) -> dict[int, tuple[str | None, str | None]]:
    """Slot -> (material, colour) of the file the article holds now."""
    return {
        row.slot: (row.material, row.color_hex)
        for row in session.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    }


def same_filament_kind(a: tuple[str | None, str | None] | None, b: tuple[str | None, str | None] | None) -> bool:
    """Two slots are of the same kind: the same material and the same colour (`(material, colour)` of each).

    The one comparison of the program for "the same filament in a slot": a slot keeps its filament
    group after another print file is chosen (Spec 5.1), and the filament page of a new article starts
    from the group of a similar article (Spec 5.2, p156; `filament.suggest_from_similar`). `None` is
    "no such slot": it equals only itself."""
    return a == b


def slots_losing_group(session: Session, article_id: int, requirements: Sequence[FilamentRequirement]) -> list[int]:
    """Slots of the new file the person has to tick again after a change of the file (Spec 5.1).

    Empty when no ticked group is lost: a slot that never had a group is no news. When a group is lost
    (the slot is another material or colour now, or the new file has no such slot), every slot of the new
    file without a kept group is named. Read before the change; the same rule as `_take_over_filaments`."""
    old = _filament_kinds(session, article_id)
    ticked = set(
        session.scalars(select(ArticleFilamentSort.slot).where(ArticleFilamentSort.article_id == article_id))
    )
    new = {r.slot: (r.material, r.color_hex) for r in requirements}
    kept = {slot for slot in ticked if same_filament_kind(old.get(slot), new.get(slot))}
    if ticked <= kept:
        return []
    return sorted(slot for slot in new if slot not in kept)


def _take_over_filaments(
    session: Session,
    article_id: int,
    requirements: Sequence[FilamentRequirement],
) -> None:
    """`_replace_filaments`, left out of the activity log: a new print file is the journal's (Spec 10.7)."""
    with quiet(session):
        _replace_filaments(session, article_id, requirements)


def _replace_filaments(
    session: Session,
    article_id: int,
    requirements: Sequence[FilamentRequirement],
) -> None:
    """Replace the filament rows. A slot keeps its filament group (the ticked spool sorts, Spec 5.2)
    when material and colour stay the same (plan B4 decision 10; for every choice of a file, Spec 5.1);
    every other group of the article is dropped, so that a slot never counts spool sorts chosen for
    another colour."""
    old = _filament_kinds(session, article_id)
    new = {r.slot: (r.material, r.color_hex) for r in requirements}
    keep = {slot for slot, value in new.items() if same_filament_kind(old.get(slot), value)}
    _delete_rows(
        session,
        select(ArticleFilamentSort).where(
            ArticleFilamentSort.article_id == article_id, ArticleFilamentSort.slot.not_in(keep)
        ),
    )
    _delete_rows(session, select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    for requirement in requirements:
        session.add(
            ArticleFilament(
                article_id=article_id,
                slot=requirement.slot,
                grams=requirement.grams,
                material=requirement.material,
                color_hex=requirement.color_hex,
            )
        )


def _active_article(session: Session, article_id: int) -> Article:
    article = _article_or_404(session, article_id)
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    return article


def _lift_file_pause(article: Article, reasons: tuple[str, ...]) -> None:
    if article.paused and article.pause_reason in reasons:
        article.paused = False
        article.pause_reason = None
        article.consecutive_failures = 0


def confirm_article_file(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
    actor: Actor,
) -> Article:
    """Spec 5.1: accept the changed file as it is now; lifts a `file_changed` pause.

    A `file_missing` pause stays (the file is not there); other pauses stay too.
    """
    article = _active_article(session, article_id)
    if file.id != article.library_file_id:
        raise ConflictError("articles.confirm_other_file")
    details = {
        "old_name": article.file_name,
        "old_fingerprint": article.file_fingerprint,
        "new_name": file.filename,
        "new_fingerprint": file.fingerprint,
    }
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    _lift_file_pause(article, ("file_changed",))
    session.flush()
    write_journal(
        session, kind="article_file_confirmed", actor=actor, article_id=article.id, details=details
    )
    return article


def relink_article(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    plate: Plate,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
    actor: Actor,
) -> Article:
    """Spec 5.1: choose another file and plate for an active article; lifts a file pause."""
    article = _active_article(session, article_id)
    holder = _plate_holder(session, file.id, plate.index, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.plate_taken", name=holder.name)
    details = {
        "old_file_id": article.library_file_id,
        "old_plate": article.plate_id,
        "new_file_id": file.id,
        "new_plate": plate.index,
    }
    article.library_file_id = file.id
    article.plate_id = plate.index
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    _lift_file_pause(article, ("file_changed", "file_missing"))
    session.flush()
    write_journal(
        session, kind="article_relinked", actor=actor, article_id=article.id, details=details
    )
    return article


def archive_part(session: Session, part_id: int) -> None:
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    _must_be_active(part)
    user = session.scalar(
        select(Article)
        .join(ArticlePart, ArticlePart.article_id == Article.id)
        .where(ArticlePart.part_id == part_id, articlesets.where_catalog())
        .order_by(Article.name)
        .limit(1)
    )
    if user is not None:
        raise ConflictError("parts.archive_in_use", article=user.name)
    part.archived_at = utcnow()
    session.flush()


def restore_part(session: Session, part_id: int) -> Part:
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    _must_be_archived(part)
    part.archived_at = None
    session.flush()
    return part


def archive_set(session: Session, set_id: int) -> None:
    """A set can be archived unless an active set holds it on any level (Spec 5.4, 5.5; 0.4.3): not
    only directly, since a restored set may hold an archived one (review finding 2). Open orders keep
    their lines and still ship."""
    product_set = get_set(session, set_id)
    _must_be_active(product_set)
    parents = sets_holding_set(session, set_id)
    if parents:
        raise ConflictError("sets.archive_in_set", names=", ".join(parents))
    product_set.archived_at = utcnow()
    session.flush()


def restore_set(session: Session, set_id: int) -> ProductSet:
    product_set = get_set(session, set_id)
    _must_be_archived(product_set)
    product_set.archived_at = None
    session.flush()
    return product_set
