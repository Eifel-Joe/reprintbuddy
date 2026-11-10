"""Assigning a name that is no column of a table fails at once instead of being dropped silently."""

import pytest

from reprintbuddy.models import Base, Part, PartSupplier

from factories import make_article


def test_assigning_a_removed_column_raises():
    part = Part(name="Screw")
    with pytest.raises(AttributeError, match="pack_size"):
        part.pack_size = 5  # moved to PartSupplier in migration 0015


def test_assigning_a_column_still_works():
    part = Part(name="Screw")
    part.stock = 3
    supplier = PartSupplier(position=0)
    supplier.pack_size = 5
    assert (part.stock, supplier.pack_size) == (3, 5)


def test_private_names_pass(db):
    with db.begin() as s:
        article = make_article(s)
        article._scratch = 1  # private names stay free, SQLAlchemy keeps its own state under _sa_*
        assert article._scratch == 1


def test_constructor_rejects_unknown_keywords():
    with pytest.raises(TypeError):
        Part(name="Screw", pack_size=5)


def test_guard_covers_every_table():
    """A table added later is guarded without further work: the check sits on the shared base."""
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        with pytest.raises(AttributeError):
            cls.__new__(cls).__setattr__("no_such_field", 1)


def test_a_backref_added_when_the_mappers_are_configured_is_allowed():
    """Review C 7: the allowed names are taken from configured mappers, so a backref counts too."""
    from sqlalchemy import ForeignKey
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

    from reprintbuddy import models

    class Local(DeclarativeBase):
        __extra_attrs__ = frozenset()

    class Parent(Local):
        __tablename__ = "parent"
        id: Mapped[int] = mapped_column(primary_key=True)

    class Child(Local):
        __tablename__ = "child"
        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))
        parent = relationship(Parent, backref="children")

    # the first look comes before anything else configured the mappers
    assert "children" in models._settable(Parent)
    assert "parent" in models._settable(Child)
