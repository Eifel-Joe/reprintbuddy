"""Every text in the language files is used somewhere (v0.3.8 p74), so the files do not collect dead texts.

A key counts as used when it stands in the source as a quoted literal ("a.b" or 'a.b'), when it is
the base of a plural pair (`a.b.one` / `a.b.other` are read as `a.b` by `translate_count`/`tc`), or when
it belongs to a family whose keys the code builds from a state or a code (`f"pause.reason.{reason}"`).

Each family below names the place that builds it; the test checks that this place still exists, so a
family cannot outlive its code. Since v0.3.9 (p121) a family does not let every key with its prefix
through: the endings (the part after the prefix) that the code can build are taken from the code itself
(a `Literal`, a table, the calls of the builder), and a key of the family that nothing reads as a literal
is used only when its ending is among them. A family whose endings cannot be taken from the code goes
into `NOT_DERIVABLE`, with the reason. A new text that nothing reads fails the test: use it or remove it.
"""

import inspect
import json
import re
from pathlib import Path
from typing import get_args

import pytest

import reprintbuddy
from reprintbuddy.bambuddy import connection, live
from reprintbuddy.domain import engine, status as domain_status
from reprintbuddy.domain.pauses import PAUSE_REASONS
from reprintbuddy.domain.stock import KINDS, Actor
from reprintbuddy.domain.workqueue import Origin
from reprintbuddy.labels import fields as label_fields, status as label_status
from reprintbuddy.models import Article, Dispatch
from reprintbuddy.ops import fullbackup
from reprintbuddy.ops.status import UPDATE_STEPS, UpdateState
from reprintbuddy.web.routes import ops as ops_routes, settings as settings_routes

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
    "labels.cap.": 'f"labels.cap.{key}"',  # word in front of a number on a label
    "labels.field.": 'f"labels.field.{key}"',  # one name per detail a label can carry
    "labels.problem.": 'f"labels.problem.{problem.key}"',  # what the printer reports before a print
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
    "upload.error.": 'f"upload.error.{error.code}"',  # why a print file is not sent (Spec 5.1a)
    "warning.": 'f"warning.{key}"',  # warnings of the engine
}

# prefix -> why the endings cannot be taken from the code (these families let every key through, as
# before p121). Empty: every family has a place in the code that says which endings are possible.
NOT_DERIVABLE: dict[str, str] = {}

# Journal kinds that are not booked through `write_journal` (so they are not in `KINDS`): a piece of
# source that writes them. The second one was written into old journals by a migration.
JOURNAL_KINDS_WRITTEN_ELSEWHERE = {
    "filament_sorts_converted": 'kind="filament_sorts_converted"',  # domain/filament.py adds the entry itself
    "filament_sorts_merged": "'auto', 'filament_sorts_merged'",  # migration 0006
}

# key -> why nothing reads it. A finding to settle by hand (use the text or remove it); the entry goes
# with the key.
KNOWN_DEAD: dict[str, str] = {
    "ops.update.state.idle": "nothing shows it (ops_view builds no key for 'idle'); kept because test_ops_update demands a text for every state",
    "stock.limit.part": "nothing raises it (purchased parts are clamped to 0, PartShortage); kept because test_domain_error_keys demands a text for every field",
}

_sources_cache: list[str] = []


def _sources():
    if not _sources_cache:  # the same text for every test: reading it takes seconds
        parts = []
        for path in sorted(PACKAGE.rglob("*")):
            if path.suffix in {".py", ".html", ".js"} and "locales" not in path.parts and "__pycache__" not in path.parts:
                parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        _sources_cache.append("\n".join(parts) + "\n")
    return _sources_cache[0]


def _literals(text):
    """Everything that stands alone between two quotes ("a.b", 'a.b'); pairs of quotes may overlap."""
    return set(re.findall(r"""(?=["']([^"'\s]+)["'])""", text))


def _constraint_values(model, name):
    """The quoted values of the CHECK constraint `name` of a table (`status IN ('a', 'b')`)."""
    (constraint,) = [c for c in model.__table__.constraints if c.name == name]
    return set(re.findall(r"'(\w+)'", str(constraint.sqltext)))


def _article_counters():
    """The counters of an article that cannot go below 0 (CHECK `<field> >= 0`): what a booking can hit."""
    found = set()
    for constraint in Article.__table__.constraints:
        match = re.fullmatch(r"(\w+) >= 0", str(getattr(constraint, "sqltext", "")))
        if match:
            found.add(match.group(1))
    return found


def _found(pattern, text):
    """The values a pattern finds in source; none means the source changed and this must change with it."""
    values = set(re.findall(pattern, text))
    assert values, f"nothing found for {pattern}"
    return values


def _error_kinds(text):
    return _found(r'error_response\(\s*request,\s*[\w.]+,\s*"(\w+)"\)', text)


def _update_states_shown():
    shown = inspect.getsource(ops_routes.ops_view)
    assert 'state != "idle"' in shown, "ops_view shows idle now: take the exception out of the test"
    return set(get_args(UpdateState)) - {"idle"}  # ops_view builds no key for 'idle'


def _channel_states():
    return _found(r'(?:self\.state = |self\._state = |_pause\(\s*)"(\w+)"', inspect.getsource(live))


def _automation_fields():
    source = inspect.getsource(settings_routes.save_automation)
    (names,) = _found(r"numbers = \{.*? for name in \(([^)]*)\)\}", source)
    return set(re.findall(r'"(\w+)"', names))


# prefix -> the endings the code can build (a function of the source text)
DERIVED = {
    "bambuddy.reason.": lambda text: set(get_args(connection.Reason)),
    "bambuddy.right.": lambda text: set(connection.KEY_RIGHT_FOR.values()),
    "dispatch.status.": lambda text: _constraint_values(Dispatch, "ck_dispatches_status"),
    "error.": lambda text: {f"{kind}.{part}" for kind in _error_kinds(text) for part in ("title", "message")},
    "journal.actor.": lambda text: set(get_args(Actor)),
    "journal.kind.": lambda text: set(KINDS)
    | {kind for kind, fragment in JOURNAL_KINDS_WRITTEN_ELSEWHERE.items() if fragment in text},
    "keyright.": lambda text: {right for right, _english in connection.KEY_SETTINGS_ON},
    "labels.cap.": lambda text: set(label_fields._CAPTIONED),
    "labels.field.": lambda text: set(label_fields.FIELDS),
    "labels.problem.": lambda text: _found(r'Problem\(\s*"(\w+)"', inspect.getsource(label_status)),
    "ops.msg.": lambda text: set(ops_routes.OPS_MESSAGES),
    "ops.restore.": lambda text: {fullbackup.PasswordNeeded.code, fullbackup.WrongPassword.code},
    "ops.update.state.": lambda text: _update_states_shown(),
    "ops.update.step.": lambda text: set(UPDATE_STEPS),
    "pause.reason.": lambda text: set(PAUSE_REASONS),
    "permission.": lambda text: set(connection.EDITOR_LABELS)
    | set(connection.KEY_CREATION_LABELS)
    | set(connection.REQUIRED_PERMISSIONS),
    "queue.origin.": lambda text: set(get_args(Origin)),
    "settings.channel.": lambda text: {*_channel_states(), *(f"badge.{state}" for state in _channel_states())},
    "settings.invalid_": lambda text: _automation_fields(),
    "status.": lambda text: {f"{kind}.{state}" for kind, states in domain_status.STATES.items() for state in states},
    "stock.limit.": lambda text: _article_counters(),
    "warning.": lambda text: _found(r'\badd\(\s*"(\w+)"', inspect.getsource(engine.collect_warnings)),
}


def unused_keys(keys, text):
    """The keys that nothing in the source `text` reads.

    A key of a family that is not a literal is read only when its ending is one the code can build.
    """
    literals = _literals(text)
    endings = {prefix: derive(text) for prefix, derive in DERIVED.items()}
    unused = []
    for key in keys:
        if key in literals:
            continue
        base = key.rsplit(".", 1)[0]
        if key.endswith((".one", ".other")) and base in literals:
            continue
        if any(
            key.startswith(prefix) and (prefix in NOT_DERIVABLE or key[len(prefix) :] in endings[prefix])
            for prefix in FAMILIES
        ):
            continue
        unused.append(key)
    return unused


def _keys():
    return json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))


def test_the_language_files_have_the_same_keys():
    de = json.loads((LOCALES / "de.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    assert set(de) == set(en)


def test_every_family_still_has_its_code_and_its_keys():
    text = _sources()
    keys = _keys()
    for prefix, fragment in FAMILIES.items():
        assert fragment in text, f"no code builds {prefix}* any more: {fragment}"
        assert any(key.startswith(prefix) for key in keys), f"no key starts with {prefix} any more"


def test_every_family_has_its_endings_from_the_code_or_a_reason_why_not():
    assert set(FAMILIES) == set(DERIVED) | set(NOT_DERIVABLE)
    assert not set(DERIVED) & set(NOT_DERIVABLE)
    assert all(NOT_DERIVABLE.values()), "a family that is not derived needs its reason"
    text = _sources()
    for prefix, derive in DERIVED.items():
        assert derive(text), f"the code gives no endings for {prefix}*"


@pytest.mark.parametrize("prefix", [prefix for prefix in FAMILIES if prefix not in NOT_DERIVABLE])
def test_a_dead_text_in_a_family_is_found(prefix):
    dead = f"{prefix}zz_dead"
    assert set(unused_keys([*_keys(), dead], _sources())) - set(KNOWN_DEAD) == {dead}


def test_a_family_that_is_not_derived_lets_every_ending_through():
    for prefix in NOT_DERIVABLE:
        assert f"{prefix}zz_dead" not in unused_keys([f"{prefix}zz_dead"], _sources())


def test_every_text_is_used():
    assert set(unused_keys(_keys(), _sources())) - set(KNOWN_DEAD) == set()


def test_a_known_dead_text_is_still_there_and_still_dead():
    """Once a text is used or removed, its entry goes."""
    keys = _keys()
    assert set(KNOWN_DEAD) <= set(keys)
    assert set(KNOWN_DEAD) <= set(unused_keys(keys, _sources()))
