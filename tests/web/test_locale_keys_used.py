"""Every text in the language files is used somewhere (v0.3.8 p74), so the files do not collect dead texts.

A key counts as used when it stands in the source as a quoted literal ("a.b" or 'a.b'), when it is
the base of a plural pair (`a.b.one` / `a.b.other` are read as `a.b` by `translate_count`/`tc`), or when
it belongs to a family whose keys the code builds from a state or a code (`f"pause.reason.{reason}"`).
Each family below names the place that builds it; the test checks that this place still exists, so a
family cannot outlive its code. A new text that nothing reads fails the test: use it or remove it.
"""

import json
import re
from pathlib import Path

import reprintbuddy

PACKAGE = Path(reprintbuddy.__file__).parent
LOCALES = PACKAGE / "locales"

# prefix of the keys -> a piece of source that builds them
FAMILIES = {
    "bambuddy.reason.": 'f"bambuddy.reason.{check.reason}"',  # the reason a connection check failed
    "bambuddy.right.": "'bambuddy.right.' ~",  # one text per right the key owner needs
    "dispatch.status.": "'dispatch.status.' ~",  # state of a hand-over
    "error.": 'f"error.{kind}.title"',  # error page: not_found, forbidden, host, generic
    "journal.actor.": 'f"journal.actor.{entry.actor}"',
    "journal.kind.": 'f"journal.kind.{entry.kind}"',  # one text per kind of booking
    "keyright.": "'keyright.' ~",  # rights of the key on the setup page
    "ops.msg.": 'f"ops.msg.{code}"',  # message codes of the maintenance actions
    "ops.restore.": 'f"ops.restore.{error.code}"',
    "ops.update.state.": 'f"ops.update.state.{view.update.state}"',
    "ops.update.step.": 'f"ops.update.step.{view.update.step}"',
    "pause.reason.": 'f"pause.reason.{reason}"',
    "permission.": "'permission.' ~ code",  # one explanation per Bambuddy right
    "queue.origin.": "'queue.origin.' ~",
    "settings.channel.": "'settings.channel.' ~",  # state of the live channel
    "settings.invalid_": 'f"settings.invalid_{name}"',  # one error per automation field
    "status.": 'f"status.{kind}.{state}"',  # badges per kind and state
    "stock.limit.": 'f"stock.limit.{field}"',
    "warning.": 'f"warning.{key}"',  # warnings of the engine
}


def _sources():
    text = ""
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix in {".py", ".html", ".js"} and "locales" not in path.parts and "__pycache__" not in path.parts:
            text += path.read_text(encoding="utf-8", errors="ignore") + "\n"
    return text


def _is_literal(key, text):
    return re.search(r"""["']""" + re.escape(key) + r"""["']""", text) is not None


def test_the_language_files_have_the_same_keys():
    de = json.loads((LOCALES / "de.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    assert set(de) == set(en)


def test_every_family_still_has_its_code_and_its_keys():
    text = _sources()
    keys = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    for prefix, fragment in FAMILIES.items():
        assert fragment in text, f"no code builds {prefix}* any more: {fragment}"
        assert any(key.startswith(prefix) for key in keys), f"no key starts with {prefix} any more"


def test_every_text_is_used():
    text = _sources()
    keys = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    unused = []
    for key in keys:
        if _is_literal(key, text):
            continue
        base = key.rsplit(".", 1)[0]
        if key.endswith((".one", ".other")) and _is_literal(base, text):
            continue
        if any(key.startswith(prefix) for prefix in FAMILIES):
            continue
        unused.append(key)
    assert unused == []
