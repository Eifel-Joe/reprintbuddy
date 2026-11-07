"""The hint "waiting for approval" names the group of the key's owner, not a key (v0.3.8 p111).

Bambuddy (backend/app/core/auth.py, `queue_review_required_for`): a job waits for someone to start it
when the user who owns the API key lacks `queue:start_unreviewed` and `queue:update_all`. The right
comes from that user's group, and a key's rights cannot be changed afterwards in Bambuddy, so the
text must not advise a new key.
"""

import pytest

from reprintbuddy.domain.engine import EngineWarning, warning_text


@pytest.mark.parametrize("lang", ["en", "de"])
def test_hint_names_the_group_and_the_right(lang):
    text = warning_text(lang, EngineWarning("warning.waiting_for_review", {"count": 2}))
    assert "2" in text
    assert "queue:start_unreviewed" in text and "queue:update_all" in text
    assert "Print Without Review" in text
    assert ("group" if lang == "en" else "Gruppe") in text


@pytest.mark.parametrize("lang", ["en", "de"])
def test_hint_does_not_advise_a_key(lang):
    text = warning_text(lang, EngineWarning("warning.waiting_for_review", {"count": 1}))
    # The key is only named as the thing whose owner counts; no "give the key the right", no new key.
    assert "new key" not in text and "neuen Schlüssel" not in text
    assert "key the right" not in text and "Schlüssel das Recht" not in text
    assert "Schlüssel von Reprintbuddy das Recht" not in text and "Reprintbuddy's key the right" not in text
    # The way out that remains: start the print in Bambuddy.
    assert "in Bambuddy" in text
