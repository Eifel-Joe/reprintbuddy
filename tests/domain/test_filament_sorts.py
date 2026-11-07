"""Which sort a spool belongs to: material, subtype and a similar colour, never the brand (Spec 9.1)."""

from datetime import datetime, timedelta
from itertools import permutations

from sqlalchemy import func, select

from factories import make_filament_settings, make_sort
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import color, filament, reorder
from reprintbuddy.models import FilamentSort, ReorderMark

BLACK, NEAR_BLACK, ANTHRACITE = "#000000", "#1A1A1A", "#333333"  # 0 / 5.5 / 13.4 apart from black
RED, DARK_RED = "#FF0000", "#B30000"  # 16 apart
NOW = datetime(2026, 11, 6, 12, 0)


def sort_count(s):
    return s.scalar(select(func.count()).select_from(FilamentSort))


def test_similar_colours_form_one_sort_whatever_the_brand(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PETG", brand="Brand One", color_hex=BLACK, color_name="Black")
    b = fake.add_spool("PETG", brand="Brand Two", color_hex=NEAR_BLACK, color_name="Schwarz")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a, b])
        assert ids[a.id] == ids[b.id] and sort_count(s) == 1
        sort = s.get(FilamentSort, ids[a.id])
        assert sort.color_hex == BLACK  # the anchor: colour of the lowest-id spool
        assert sort.label == "PETG Black" and sort.color_name == "Black"  # name of the lowest-id spool with one


def test_spec_examples_apart(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA", color_hex=BLACK),
        fake.add_spool("PLA", color_hex=ANTHRACITE),
        fake.add_spool("PLA", color_hex=RED),
        fake.add_spool("PLA", color_hex=DARK_RED),
    ]
    with db.begin() as s:
        assert len(set(filament.sync_sorts(s, spools).values())) == 4


def test_no_chain_effect_and_no_dependence_on_order(db):
    """A~B and B~C but A not~C: the anchor is fixed, so C is a sort of its own, in every order."""
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_hex="#000000")
    b = fake.add_spool("PLA", color_hex="#101010")
    c = fake.add_spool("PLA", color_hex="#202020")
    assert color.similar(a.color_hex, b.color_hex) and color.similar(b.color_hex, c.color_hex)
    assert not color.similar(a.color_hex, c.color_hex)
    results = set()
    for order in permutations([a, b, c]):
        with db.begin() as s:
            s.query(FilamentSort).delete()
        with db.begin() as s:
            ids = filament.sync_sorts(s, list(order))
            results.add((ids[a.id] == ids[b.id], ids[b.id] == ids[c.id], ids[a.id] == ids[c.id]))
    assert results == {(True, False, False)}  # A and B together, C apart, whatever the list order


def test_a_second_call_gives_the_same_answer(db):
    """X is within 7 of anchor A, but nearer to anchor B, which a later spool creates: the first call
    already puts X to B, so a second call (and `resolve_sorts`) change nothing."""
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_hex="#000000")
    x = fake.add_spool("PLA", color_hex="#1D1D1D")  # 6.5 from A
    y = fake.add_spool("PLA", color_hex="#262626")  # 9.3 from A, 2.8 from X
    assert color.similar(a.color_hex, x.color_hex) and not color.similar(a.color_hex, y.color_hex)
    assert color.delta_e(x.color_hex, y.color_hex) < color.delta_e(x.color_hex, a.color_hex)
    spools = [a, x, y]
    with db.begin() as s:
        first = filament.sync_sorts(s, spools)
    with db.begin() as s:
        assert filament.sync_sorts(s, spools) == first
        assert filament.resolve_sorts(s, spools) == first
        assert sort_count(s) == 2
    assert first[a.id] != first[x.id] and first[x.id] == first[y.id]


def test_a_spool_goes_to_the_nearest_anchor(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        far = make_sort(s, color_hex="#000000", color_name="Far")
        near = make_sort(s, color_hex="#101010", color_name="Near")
        spool = fake.add_spool("PLA", color_hex="#0C0C0C")  # within 7 of both, nearer to #101010
        assert color.delta_e("#0C0C0C", "#101010") < color.delta_e("#0C0C0C", "#000000") <= color.SIMILAR
        assert filament.sync_sorts(s, [spool])[spool.id] == near.id
        assert near.id != far.id and sort_count(s) == 2


def test_anchor_does_not_move_when_the_spool_set_changes(db):
    fake = FakeBambuddy()
    first = fake.add_spool("PLA", color_hex="#000000")
    second = fake.add_spool("PLA", color_hex="#101010")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [first, second])
        sort_id = ids[first.id]
    with db.begin() as s:  # the first spool is gone (archived): the sort and its anchor stay
        ids = filament.sync_sorts(s, [second])
        assert ids[second.id] == sort_id
        assert s.get(FilamentSort, sort_id).color_hex == "#000000" and sort_count(s) == 1


def test_a_new_spool_joins_an_existing_sort_in_the_next_call(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_hex=BLACK, color_name="Black")
    with db.begin() as s:
        first = filament.sync_sorts(s, [a])[a.id]
    b = fake.add_spool("PLA", brand="Other Brand", color_hex=NEAR_BLACK)
    with db.begin() as s:
        assert filament.sync_sorts(s, [a, b])[b.id] == first and sort_count(s) == 1


def test_spools_without_a_colour(db):
    fake = FakeBambuddy()
    none1 = fake.add_spool("PLA", color_hex=None, color_name="Natural")
    none2 = fake.add_spool("PLA", color_hex=None)
    blank = fake.add_spool("PLA", color_hex="")
    black = fake.add_spool("PLA", color_hex=BLACK)
    word = fake.add_spool("PLA", color_hex="Natural")
    other_word = fake.add_spool("PLA", color_hex="Clear")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [none1, none2, blank, black, word, other_word])
        assert ids[none1.id] == ids[none2.id] == ids[blank.id]  # no colour at all: one sort
        assert len({ids[none1.id], ids[black.id], ids[word.id], ids[other_word.id]}) == 4
        assert s.get(FilamentSort, ids[none1.id]).label == "PLA Natural"


def test_label_shows_the_colour_as_hash_rrggbb(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PETG", color_hex="000000FF")  # no hash, with an alpha part
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a])
        sort = s.get(FilamentSort, ids[a.id])
        assert sort.label == "PETG #000000" and sort.color_hex == "000000FF"  # the anchor stays as read


def test_transparent_spools_form_one_sort_apart_from_every_opaque_colour(db):  #
    fake = FakeBambuddy()
    black = fake.add_spool("PETG", color_hex="000000")
    clear_a = fake.add_spool("PETG", color_hex="00000000")
    clear_b = fake.add_spool("PETG", color_hex="FFFFFF00", brand="Other Brand")  # its RGB does not matter
    clear_other = fake.add_spool("PETG", subtype="HF", color_hex="00000000")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [black, clear_a, clear_b, clear_other])
        assert ids[clear_a.id] == ids[clear_b.id] != ids[black.id]
        assert ids[clear_other.id] not in (ids[clear_a.id], ids[black.id])  # another subtype
        assert s.get(FilamentSort, ids[clear_a.id]).label == "PETG transparent"
        assert s.get(FilamentSort, ids[black.id]).label == "PETG #000000"


def test_transparent_second_call_and_resolve_agree(db):
    fake = FakeBambuddy()
    spools = [fake.add_spool("PETG", color_hex=h) for h in ("00000000", "000000", "FFFFFF00", "1A1A1A")]
    with db.begin() as s:
        first = filament.sync_sorts(s, spools)
    with db.begin() as s:
        assert filament.sync_sorts(s, spools) == first == filament.resolve_sorts(s, spools)
        assert sort_count(s) == 2


def test_partly_transparent_spools_keep_to_their_alpha(db):
    fake = FakeBambuddy()
    half_a = fake.add_spool("PETG", color_hex="#00000080")
    half_b = fake.add_spool("PETG", color_hex="#1A1A1A80")
    other = fake.add_spool("PETG", color_hex="#00000081")
    solid = fake.add_spool("PETG", color_hex="#000000")
    clear = fake.add_spool("PETG", color_hex="#00000000")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [half_a, half_b, other, solid, clear])
        assert ids[half_a.id] == ids[half_b.id]
        assert len({ids[half_a.id], ids[other.id], ids[solid.id], ids[clear.id]}) == 4


def test_a_named_transparent_sort_shows_its_name(db):
    fake = FakeBambuddy()
    spool = fake.add_spool("PETG", color_hex="00000000", color_name="Clear")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [spool])
        assert s.get(FilamentSort, ids[spool.id]).label == "PETG Clear"


def test_a_fully_opaque_alpha_changes_nothing(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_hex="#000000FF")
    b = fake.add_spool("PLA", color_hex="#000000")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a, b])
        assert ids[a.id] == ids[b.id]


def test_resolve_sorts_reads_only(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_hex=BLACK)
    b = fake.add_spool("PLA", color_hex=NEAR_BLACK, brand="Other Brand")
    c = fake.add_spool("PETG", color_hex=BLACK)
    with db.begin() as s:
        assert filament.resolve_sorts(s, [a, b, c]) == {a.id: None, b.id: None, c.id: None}
        assert sort_count(s) == 0
        sort_id = filament.sync_sorts(s, [a])[a.id]
        assert filament.resolve_sorts(s, [a, b, c]) == {a.id: sort_id, b.id: sort_id, c.id: None}


def test_unnamed_spool_never_erases_a_known_name(db):
    fake = FakeBambuddy()
    named = fake.add_spool("PLA", color_name="Black")
    unnamed = fake.add_spool("PLA", brand="Other Brand")
    for order in ([named, unnamed], [unnamed, named]):
        with db.begin() as s:
            ids = filament.sync_sorts(s, order)
            sort = s.get(FilamentSort, ids[named.id])
            assert (sort.label, sort.color_name) == ("PLA Black", "Black")


def test_stock_adds_up_over_brands(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PETG", brand="Brand One", remaining_g=400.0),
        fake.add_spool("PETG", brand="Brand Two", color_hex=NEAR_BLACK, remaining_g=250.0),
    ]
    with db.begin() as s:
        (entry,) = filament.stock_by_sort(s, spools).values()
        assert (entry.stock_g, entry.spools, entry.label) == (650.0, 2, "PETG #000000")


def test_snapshot_rows_of_two_brands_belong_to_one_sort(db):
    fake = FakeBambuddy()
    spools = [fake.add_spool("PETG", brand="Brand One"), fake.add_spool("PETG", brand="Brand Two")]
    with db.begin() as s:
        filament.record_snapshot(s, spools, NOW.date())
    with db.begin() as s:
        stock, day = filament.stock_from_snapshots(s)
        (entry,) = stock.values()
        assert (day, entry.spools) == (NOW.date(), 2)


def _ordered_sort(db, fake, old):
    with db.begin() as s:
        filament.sync_sorts(s, [old])
        sort = s.query(FilamentSort).one()
        make_filament_settings(s, sort.id, pack_grams=1000)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        mark.marked_at = NOW - timedelta(days=1)


def test_filament_mark_closes_with_a_spool_of_another_brand(db):
    fake = FakeBambuddy()
    old = fake.add_spool("PETG", brand="Brand One", created_at=NOW - timedelta(days=9))
    _ordered_sort(db, fake, old)
    new = fake.add_spool("PETG", brand="Brand Two", color_hex=NEAR_BLACK, created_at=NOW - timedelta(hours=2))
    with db.begin() as s:
        assert reorder.close_arrived_marks(s, [old, new], NOW) == 1
        assert s.query(ReorderMark).one().received_at == NOW


def test_filament_mark_not_closed_by_another_colour(db):
    fake = FakeBambuddy()
    old = fake.add_spool("PETG", created_at=NOW - timedelta(days=9))
    _ordered_sort(db, fake, old)
    other = fake.add_spool("PETG", color_hex=RED, created_at=NOW - timedelta(hours=2))
    with db.begin() as s:
        assert reorder.close_arrived_marks(s, [old, other], NOW) == 0
