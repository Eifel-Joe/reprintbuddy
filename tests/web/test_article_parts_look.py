"""The purchased parts block of the article form in the new look: the remove control is a symbol
button with a 44 px target and an accessible name; the rows follow the settings-row look."""

import re

from conftest import sm_of
from factories import make_article, make_part
from reprintbuddy.web.i18n import translate

CSS = "/static/css/reprintbuddy.css"


def en(key, **params):
    return translate("en", key, **params)


def rule_of(css, selector):
    """The declarations of the last rule that has exactly this selector list (outside @media)."""
    found = re.findall(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", css)
    assert found, selector
    return found[-1]


def test_remove_control_is_an_icon_button_with_a_name(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert M4")
        article_id = make_article(s, name="Template A").id
    page = ready_client.get(f"/articles/{article_id}/edit").text
    removes = re.findall(r"<button[^>]*data-parts-remove[^>]*>.*?</button>", page, re.S)
    assert len(removes) == 2  # the row of the form and the one of the template for the script
    for button in removes:
        assert "icon-btn" in re.search(r'class="([^"]*)"', button).group(1).split()
        assert 'type="submit"' in button and 'name="parts_remove"' in button
        assert f'title="{en("articles.part_remove")}"' in button
        assert en("articles.part_remove") in button  # the word stays part of the accessible name
        assert "#x" in button  # the cross of the sprite


def test_css_remove_control_has_a_44px_target(ready_client):
    css = ready_client.get(CSS).text
    rule = rule_of(css, ".part-row .icon-btn")
    assert "min-width: 2.75rem" in rule and "min-height: 2.75rem" in rule
    # the visible word is hidden on every width, only the cross shows
    assert re.search(r"\.part-row \.icon-btn > span:not\(\[data-parts-name\]\) \{[^}]*clip-path", css)


def test_css_fields_of_the_block_are_tap_targets_and_quantity_is_narrow(ready_client):
    css = ready_client.get(CSS).text
    rule = rule_of(css, ".part-row select, .part-row input")
    assert "min-height: 2.75rem" in rule
    assert re.search(r"\.part-row input \{[^}]*width: 6rem", css)
    assert "min-height: 2.75rem" in rule_of(css, ".parts-list > .btn")


def test_css_rows_look_like_settings_rows(ready_client):
    css = ready_client.get(CSS).text
    rule = rule_of(css, ".part-row")
    assert "border-top" in rule and "padding" in rule
