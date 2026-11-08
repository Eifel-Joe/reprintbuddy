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
    label = make(fields.FIELDS[:8])
    assert label.truncated == [] and label.dropped == ()
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
    label = make(fields.FIELDS[:8], name="N" * 120, sku="S" * 80, label_text="L" * 80, print_target="P" * 80)
    assert len(label.lines) == 8
    assert label.font_size == render.MIN_FONT_SIZE
    assert set(label.truncated) == {0, 1, 6, 7}


# ---- too many lines (p126): what does not fit in the height is left off, never run over the margin ----

TEXT_HEIGHT = DK_11209.dots_high - 2 * render._PAD_Y


def ink_rows(label):
    """The first and the last row of the picture right of the code that has a black dot."""
    pixels = label.image.load()
    rows = [y for y in range(label.image.height)
            if any(pixels[x, y] == 0 for x in range(label.code_width + 1, label.image.width))]
    return rows[0], rows[-1]


def test_all_nine_details_do_not_fit_in_the_height_so_the_last_one_is_left_off_and_named():
    label = make(fields.FIELDS)
    assert label.dropped == ("date",)  # named by its key, so the preview can say which one is missing
    assert [row.text for row in label.lines][-1] == "Shelf B3"
    assert len(label.lines) == 8
    assert label.truncated == []


def test_the_rows_of_all_nine_stay_inside_the_margin():
    label = make(fields.FIELDS)
    assert len(label.lines) * render._pitch(label.font_size) <= TEXT_HEIGHT
    first, last = ink_rows(label)
    assert first >= render._PAD_Y and last < DK_11209.dots_high - render._PAD_Y


def test_a_label_that_fits_drops_nothing():
    for count in range(0, 9):
        assert make(fields.FIELDS[:count]).dropped == ()
    assert make().dropped == ()


def test_a_detail_without_a_value_gives_no_row_so_nothing_is_left_off():
    # all nine ticked, but the date and the label text are empty: seven rows fit
    label = make(fields.FIELDS, date="", label_text="")
    assert label.dropped == () and len(label.lines) == 7


def test_the_rows_never_run_over_the_margin_however_long_the_values_are():
    long = dict(name="N " * 80, sku="S" * 80, label_text="L " * 80, print_target="P " * 80)
    for values in ({}, long):
        for chosen in (fields.FIELDS, fields.FIELDS[:8], fields.FIELDS[1:], fields.FIELDS[2:7]):
            label = make(chosen, **values)
            assert len(label.lines) * render._pitch(label.font_size) <= TEXT_HEIGHT, (chosen, values)


def test_the_ink_stays_inside_the_margin_whatever_the_values_are():
    long = dict(name="N " * 80, sku="S" * 80, label_text="L " * 80, print_target="P " * 80)
    for values in ({}, long):
        for chosen in (fields.FIELDS, fields.FIELDS[:8], fields.FIELDS[1:], fields.FIELDS[2:7]):
            first, last = ink_rows(make(chosen, **values))
            assert first >= render._PAD_Y and last < DK_11209.dots_high - render._PAD_Y, (chosen, values)


def test_a_line_break_inside_a_value_is_a_blank_not_a_new_row():
    """A value typed with line breaks (a hand-made request: the form fields are one line) would be drawn
    by the font as several rows, over the rows below it and over the margin."""
    label = make(fields.FIELDS, label_text="Row1\nRow2\nRow3", name="A\nB\nC")
    assert [row.text for row in label.lines][0] == "A B C"
    assert "Row1 Row2 Row3" in [row.text for row in label.lines]
    first, last = ink_rows(label)
    assert first >= render._PAD_Y and last < DK_11209.dots_high - render._PAD_Y


def test_the_details_left_off_are_the_last_ones_whole_not_half_a_detail():
    label = make(fields.FIELDS, name="N" * 120)  # the long name is cut to one row, still nine rows of 29
    assert label.dropped == ("date",) and label.truncated == [0]


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


def test_a_detail_with_a_long_word_in_front_stays_one_row_and_the_letters_get_smaller():
    """The old code took the largest size (46) and broke "Einheiten je Druck: 1" after "Einheiten je"."""
    label = render.render_label(data(), ("sku", "min_stock", "units_per_print"), ADDRESS, DK_11209, caption_de)
    assert [row.text for row in label.lines] == ["TA-100", "Mindestbestand: 4", "Einheiten je Druck: 1"]
    assert label.truncated == [] and label.font_size < 46
    font = render._font(label.font_size, False)
    assert all(font.getlength(row.text) <= text_width(label) for row in label.lines)


def test_the_text_is_laid_out_the_same_on_every_machine():
    """Not by raqm where a system has it (it would turn right-to-left text the right way round and
    change every width): the note about right-to-left text and the fitting rely on the basic layout.
    (Red only on a machine whose Pillow has raqm; where it has not, the basic layout is the default.)"""
    assert render._font(40, False).layout_engine == ImageFont.Layout.BASIC
    assert render._font(40, True).layout_engine == ImageFont.Layout.BASIC


@pytest.mark.parametrize("chosen", [("min_stock",), ("min_stock", "buffer", "target_qty", "units_per_print")])
def test_a_number_detail_is_never_broken_whatever_the_size(chosen):
    label = render.render_label(data(min_stock=1234567), chosen, ADDRESS, DK_11209, caption_de)
    assert len(label.lines) == len(chosen) and label.truncated == []
    assert label.lines[0].text == "Mindestbestand: 1234567"


def test_a_long_value_after_a_word_is_cut_in_its_value_not_in_its_word():
    label = render.render_label(
        data(min_stock=int("9" * 80)), ("min_stock",), ADDRESS, DK_11209, caption_de
    )
    assert len(label.lines) == 1 and label.truncated == [0]
    assert label.font_size == render.MIN_FONT_SIZE
    assert label.lines[0].text.startswith("Mindestbestand: 9") and label.lines[0].text.endswith("…")


# ---- the print target is not a number: it wraps at blanks like a free text (p128) ----

LONG_TARGET = "Any A1 mini X1C special build desk"


def test_only_the_four_numbers_are_fixed_to_one_row():
    rows = {row.text.split(":")[0]: row for row in fields.lines(data(), fields.FIELDS, caption_de)}
    for word in ("Mindestbestand", "Puffer", "Zielmenge", "Einheiten je Druck"):
        assert rows[word].fixed is True, word
    assert rows["Druckziel"].fixed is False  # a word and a text: it may take a second row


def test_a_long_print_target_wraps_at_blanks_and_nothing_is_cut():
    """Before, the whole target was one row: it was cut with an ellipsis at the smallest letters."""
    label = render.render_label(
        data(print_target=LONG_TARGET), ("sku", "print_target", "label_text"), ADDRESS, DK_11209, caption_de
    )
    assert label.truncated == [] and label.dropped == ()
    assert [row.text for row in label.lines][0] == "TA-100" and [row.text for row in label.lines][-1] == "Shelf B3"
    target_rows = [row.text for row in label.lines][1:-1]
    assert len(target_rows) == 2
    assert " ".join(target_rows) == f"Druckziel: {LONG_TARGET}"
    font = render._font(label.font_size, False)
    assert all(font.getlength(row) <= text_width(label) for row in target_rows)


def test_a_print_target_that_is_one_long_word_is_cut_not_dropped_or_run_over():
    label = render.render_label(data(print_target="P" * 80), ("print_target",), ADDRESS, DK_11209, caption_de)
    assert label.truncated == [0] and label.lines[-1].text.endswith("…")
    first, last = ink_rows(label)
    assert first >= render._PAD_Y and last < DK_11209.dots_high - render._PAD_Y


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


# ---- what the label cannot show right: the notes of the preview (p118) ----

COARSE = "https://" + "a" * 50 + ".example.org:8443/a/123456"  # 84 characters: still version 5, dots of 6
FINE = "https://" + "a" * 60 + ".example.org:8443/a/123456"  # 94 characters: version 6, dots of 5
FINER = "https://" + "a" * 100 + ".example.org/a/1"  # 124 characters: dots of 4


def test_a_character_the_font_does_not_know_is_named():
    label = make(("name",), name="Plate 中 and 日本")
    assert label.missing == ("中", "日", "本")


def test_the_characters_the_font_has_are_not_named():
    label = make(("name", "sku", "label_text"), name="Größe Ä ö ü ß × € ← ✓", label_text="Fach 3, schräg")
    assert label.missing == ()


def test_a_missing_character_is_named_once_and_blanks_do_not_count():
    assert make(("name",), name="中 中  中").missing == ("中",)


def test_bold_and_regular_text_are_both_checked():
    label = make(("name", "label_text"), name="Plain", label_text="中")  # the name is bold, the text is not
    assert label.missing == ("中",)
    assert make(("name",), name="中").missing == ("中",)


def test_a_character_that_was_cut_off_is_not_named():
    label = make(("name",), name="A" * 200 + " 中")
    assert label.truncated == [0] and label.missing == ()


def test_text_that_runs_right_to_left_is_flagged():
    hebrew = make(("name",), name="שלום")
    arabic = make(("sku",), sku="مرحبا")
    assert hebrew.rtl and arabic.rtl
    assert hebrew.missing == () and arabic.missing == ()  # the font has the letters, only the direction is wrong


def test_left_to_right_text_is_not_flagged():
    label = make(fields.FIELDS)
    assert label.rtl is False and label.missing == () and label.fine_code is False


def test_the_notes_are_empty_for_an_ordinary_label():
    label = make()
    assert (label.missing, label.rtl, label.fine_code) == ((), False, False)


def test_the_limit_of_a_fine_code_is_half_a_millimetre_at_300_dpi():
    assert render.MIN_MODULE_DOTS == 6  # 6 dots = 0.51 mm; the usual advice for a phone at arm's length is 0.5 mm


def test_a_usual_address_has_big_dots_and_no_note():
    label = make()
    assert label.module_dots == 7 and label.fine_code is False


def test_an_address_of_up_to_eighty_odd_characters_is_still_fine():
    label = render.render_label(data(), fields.DEFAULT_FIELDS, COARSE, DK_11209, caption)
    assert label.module_dots == 6 and label.fine_code is False
    assert scanned(label.image) == [COARSE]


def test_a_longer_address_makes_the_code_too_fine():
    for address, dots in ((FINE, 5), (FINER, 4)):
        label = render.render_label(data(), fields.DEFAULT_FIELDS, address, DK_11209, caption)
        assert label.module_dots == dots and label.fine_code is True


def test_the_code_alone_in_the_middle_has_the_same_dots_per_module_note():
    label = render.render_label(data(), (), FINE, DK_11209, caption)
    assert label.fine_code is True
