"""The picture of a label: the QR code of the article and the chosen details (Spec 8.5).

Always landscape: 696 x 271 dots for the 62 x 29 roll. The text size follows the number of lines
and the picture says what did not fit. The QR code is read back with a scanner library."""

import pytest
import zxingcpp
from PIL import Image, ImageFont

from reprintbuddy.labels import fields, render
from reprintbuddy.labels.rolls import DK_11209

ADDRESS = "http://203.0.113.64:8080/a/17"


def caption(key):
    return {
        "min_stock": "Minimum stock", "buffer": "Buffer", "target_qty": "Target quantity",
        "units_per_print": "Units per print", "print_target": "Print target",
    }[key]


def data(**over):
    base = dict(
        name="Template A", sku="TA-100", min_stock=4, buffer=2, target_qty=6, units_per_print=1,
        print_target="Any A1", label_text="Shelf B3", date="08.11.2026",
    )
    base.update(over)
    return fields.LabelData(**base)


def make(selected=fields.DEFAULT_FIELDS, **over):
    return render.render_label(data(**over), selected, ADDRESS, DK_11209, caption)


def scanned(image):
    return [c.text for c in zxingcpp.read_barcodes(image) if c.format == zxingcpp.BarcodeFormat.QRCode]


def dark_columns(image, x0, x1):
    pixels = image.load()
    return any(pixels[x, y] == 0 for x in range(x0, x1) for y in range(image.height))


# ---- the lines ----


def test_the_nine_details_in_their_fixed_order():
    assert fields.FIELDS == (
        "name", "sku", "min_stock", "buffer", "target_qty", "units_per_print", "print_target", "label_text", "date",
    )
    lines = fields.lines(data(), fields.FIELDS, caption)
    assert [line.text for line in lines] == [
        "Template A", "TA-100", "Minimum stock: 4", "Buffer: 2", "Target quantity: 6",
        "Units per print: 1", "Print target: Any A1", "Shelf B3", "08.11.2026",
    ]
    assert [line.bold for line in lines] == [True] + [False] * 8


def test_the_order_does_not_depend_on_the_order_chosen():
    assert [l.text for l in fields.lines(data(), ("date", "name", "sku"), caption)] == ["Template A", "TA-100", "08.11.2026"]


def test_an_empty_detail_gives_no_line():
    lines = fields.lines(data(sku=None, label_text="", print_target=None), fields.FIELDS, caption)
    assert [l.text for l in lines] == [
        "Template A", "Minimum stock: 4", "Buffer: 2", "Target quantity: 6", "Units per print: 1", "08.11.2026",
    ]


def test_numbers_are_whole_numbers_and_zero_is_a_value():
    lines = fields.lines(data(min_stock=0, buffer=0, target_qty=0), ("min_stock", "buffer", "target_qty"), caption)
    assert [l.text for l in lines] == ["Minimum stock: 0", "Buffer: 0", "Target quantity: 0"]


def test_unknown_names_are_ignored_not_trusted():
    assert fields.clean(["name", "bogus", "name", 5, None, "date"]) == ("name", "date")
    assert fields.clean("name") == ()
    assert fields.clean(None) == ()


# ---- the picture ----


def test_the_picture_is_landscape_black_and_white_and_the_size_of_the_roll():
    label = make()
    assert label.image.size == (696, 271) and label.image.mode == "1"


def test_the_code_on_the_label_says_the_articles_address():
    assert scanned(make().image) == [ADDRESS]


@pytest.mark.parametrize("selected", [(), ("name",), fields.FIELDS, ("min_stock", "date")])
def test_the_code_is_always_on_whatever_is_chosen(selected):
    assert scanned(make(selected).image) == [ADDRESS]


def test_a_longer_address_still_scans():
    long_address = "https://[2001:db8:1234:5678:9abc:def0:1234:5678]:8443/a/123456"
    label = render.render_label(data(), fields.DEFAULT_FIELDS, long_address, DK_11209, caption)
    assert scanned(label.image) == [long_address]


def test_the_text_stands_beside_the_code_not_on_it():
    label = make(("name",))
    code_width = label.code_width
    assert 150 < code_width <= 280
    assert not dark_columns(label.image, code_width, code_width + 4)  # a gap
    assert dark_columns(label.image, code_width + 8, 696)  # text there


def test_a_label_with_no_lines_has_the_code_in_the_middle():
    label = make(())
    assert label.lines == []
    assert not dark_columns(label.image, 0, 696 // 2 - label.code_width // 2 - 1)
    assert not dark_columns(label.image, 696 // 2 + label.code_width // 2 + 1, 696)


def test_fewer_lines_get_bigger_letters():
    sizes = [make(fields.FIELDS[:count]).font_size for count in (1, 2, 3, 5, 9)]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] > sizes[-1] >= render.MIN_FONT_SIZE


def test_every_line_fits_when_the_size_is_chosen_to_fit():
    label = make(fields.FIELDS)
    assert label.truncated == []
    assert label.font_size >= render.MIN_FONT_SIZE


def test_a_detail_that_cannot_fit_even_in_two_rows_at_the_smallest_size_is_cut_and_named():
    label = make(("name", "sku"), name="A very long name for a template that will never fit on a small label " * 3)
    assert label.truncated == [0]
    assert label.font_size == render.MIN_FONT_SIZE
    assert label.lines[-1].text == "TA-100"
    assert label.lines[1].text.endswith("…")  # the second row of the name ends in the ellipsis


def test_a_long_name_takes_a_second_row_and_stays_large_instead_of_shrinking():
    label = make(("name",), name="Wall panel template type A")
    assert len(label.lines) == 2 and label.truncated == []
    assert " ".join(row.text for row in label.lines) == "Wall panel template type A"
    assert label.font_size > render.MIN_FONT_SIZE


def test_nine_details_with_long_values_are_cut_to_one_row_each_when_two_rows_do_not_fit():
    label = make(fields.FIELDS, name="N" * 120, sku="S" * 80, label_text="L" * 80, print_target="P" * 80)
    assert len(label.lines) == 9
    assert label.font_size == render.MIN_FONT_SIZE
    assert set(label.truncated) == {0, 1, 6, 7}


def test_a_single_word_wider_than_a_row_is_broken_not_lost():
    label = make(("name",), name="W" * 20)
    assert label.truncated == [] and len(label.lines) == 2
    assert "".join(row.text for row in label.lines) == "W" * 20


def test_a_short_name_is_not_cut():
    assert make(("name",)).lines[0].text == "Template A"


def test_the_longest_line_that_still_fits_gets_the_largest_possible_size():
    short = make(("name",), name="Hi")
    longer = make(("name",), name="A name that is a good deal longer than that")
    assert short.font_size > longer.font_size


def test_umlauts_and_the_times_sign_are_real_glyphs_not_empty_boxes():
    font = ImageFont.truetype(str(render.FONT_REGULAR), 40)
    missing = font.getmask("")  # nothing is assigned here: the font's own "missing" box
    for char in "ÄÖÜäöüß×€":
        assert font.getmask(char).getbbox() is not None
        assert bytes(font.getmask(char)) != bytes(missing) or char == ""
    label = make(("name",), name="Größenänderung Ä ö ü ß ×")
    assert label.truncated == [] or label.lines[0].text.startswith("Größe")


def test_the_picture_is_deterministic():
    assert make().image.tobytes() == make().image.tobytes()


def test_sample_data_for_the_trial_print_and_the_preview():
    sample = fields.sample(date="08.11.2026", name="Sample label")
    assert sample.name == "Sample label"
    assert sample.sku and sample.label_text and sample.print_target
    for key in fields.FIELDS:
        assert fields.lines(sample, (key,), caption)  # every detail has something to show


# ---- a number stays beside its word; the letters get smaller instead (p124) ----


def caption_de(key):
    return {
        "min_stock": "Mindestbestand", "buffer": "Puffer", "target_qty": "Zielmenge",
        "units_per_print": "Einheiten je Druck", "print_target": "Druckziel",
    }[key]


def text_width(label):
    """The width the text may take beside the code, as `render_label` works it out."""
    return 696 - (label.code_width + render._GAP) - render._PAD_X


def test_the_live_case_keeps_the_number_beside_its_word():
    """The label of the live test: article number, "Mindestbestand: 3", label text, roll 62 x 29.
    It was drawn as "Mindestbestand" / ": 3" (a row for the word, one for the number)."""
    label = render.render_label(
        data(sku="AB-12", min_stock=3, label_text="Regal"), ("sku", "min_stock", "label_text"),
        ADDRESS, DK_11209, caption_de,
    )
    assert [row.text for row in label.lines] == ["AB-12", "Mindestbestand: 3", "Regal"]
    assert label.truncated == []
    assert label.font_size > render.MIN_FONT_SIZE
    font = render._font(label.font_size, False)
    assert all(font.getlength(row.text) <= text_width(label) for row in label.lines)


def test_every_detail_with_a_word_in_front_is_one_row_with_all_nine_chosen():
    label = render.render_label(data(), fields.FIELDS, ADDRESS, DK_11209, caption_de)
    texts = [row.text for row in label.lines]
    for expected in ("Mindestbestand: 4", "Puffer: 2", "Zielmenge: 6", "Einheiten je Druck: 1", "Druckziel: Any A1"):
        assert expected in texts


@pytest.mark.parametrize("chosen", [("min_stock",), ("min_stock", "buffer", "target_qty", "units_per_print")])
def test_a_number_detail_is_never_broken_whatever_the_size(chosen):
    label = render.render_label(data(min_stock=1234567), chosen, ADDRESS, DK_11209, caption_de)
    assert len(label.lines) == len(chosen) and label.truncated == []
    assert label.lines[0].text == "Mindestbestand: 1234567"


def test_a_long_value_after_a_word_is_cut_in_its_value_not_in_its_word():
    label = render.render_label(
        data(print_target="P" * 80), ("print_target",), ADDRESS, DK_11209, caption_de
    )
    assert len(label.lines) == 1 and label.truncated == [0]
    assert label.font_size == render.MIN_FONT_SIZE
    assert label.lines[0].text.startswith("Druckziel: P") and label.lines[0].text.endswith("…")


def test_a_long_single_word_gets_smaller_letters_instead_of_being_broken_between_letters():
    label = make(("name",), name="Kunststoffgehäusedeckel")
    assert [row.text for row in label.lines] == ["Kunststoffgehäusedeckel"]
    assert render.MIN_FONT_SIZE <= label.font_size < render.MAX_FONT_SIZE
    assert label.truncated == []


def test_the_free_text_of_the_label_is_not_broken_between_letters_either():
    label = make(("label_text",), label_text="Regalfachbeschriftung")
    assert [row.text for row in label.lines] == ["Regalfachbeschriftung"]
    assert label.font_size >= render.MIN_FONT_SIZE


def test_free_text_wraps_at_blanks_only_and_not_in_the_middle_of_a_word():
    label = make(("name",), name="Wall panel template type A")
    assert len(label.lines) == 2
    assert {word for row in label.lines for word in row.text.split()} <= set("Wall panel template type A".split())
    assert " ".join(row.text for row in label.lines) == "Wall panel template type A"
