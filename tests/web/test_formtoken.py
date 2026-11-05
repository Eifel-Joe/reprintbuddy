from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import FormToken, utcnow
from reprintbuddy.web.formtoken import (
    FormTokenInvalid,
    FormTokenUsed,
    consume_form_token,
    form_transaction,
    issue_form_token,
)

SECRET = "secret"


def token_count(s):
    return s.scalar(select(func.count()).select_from(FormToken))


def test_token_roundtrip(db):
    token = issue_form_token(SECRET)
    with db.begin() as s:
        consume_form_token(s, SECRET, token)
    with pytest.raises(FormTokenUsed):
        with db.begin() as s:
            consume_form_token(s, SECRET, token)


def test_issue_writes_nothing(db):
    issue_form_token(SECRET)
    with db.begin() as s:
        assert token_count(s) == 0


def test_tokens_differ():
    assert issue_form_token(SECRET) != issue_form_token(SECRET)


def test_token_forged(db):
    with pytest.raises(FormTokenInvalid):
        with db.begin() as s:
            consume_form_token(s, SECRET, "abc")
    foreign = issue_form_token("other secret")
    with pytest.raises(FormTokenInvalid):
        with db.begin() as s:
            consume_form_token(s, SECRET, foreign)
    with db.begin() as s:
        assert token_count(s) == 0


def test_token_expired(db):
    token = issue_form_token(SECRET)
    with pytest.raises(FormTokenInvalid):
        with db.begin() as s:
            consume_form_token(s, SECRET, token, max_age_s=-1)


def test_token_released_on_rollback(db):
    token = issue_form_token(SECRET)
    with pytest.raises(ValidationError):
        with db.begin() as s:
            consume_form_token(s, SECRET, token)
            raise ValidationError("form.required", field="x")
    with db.begin() as s:
        consume_form_token(s, SECRET, token)  # still usable


def test_old_tokens_are_cleaned_up(db):
    from datetime import timedelta

    with db.begin() as s:
        s.add(FormToken(token="old", used_at=utcnow() - timedelta(days=3)))
        s.add(FormToken(token="recent", used_at=utcnow() - timedelta(hours=1)))
    with db.begin() as s:
        consume_form_token(s, SECRET, issue_form_token(SECRET))
    with db.begin() as s:
        assert {t.token for t in s.scalars(select(FormToken))} - {"old", "recent"} != set()
        assert s.get(FormToken, "old") is None and s.get(FormToken, "recent") is not None


def make_request(db):
    app = SimpleNamespace(
        state=SimpleNamespace(sessionmaker=db, session_secret=SECRET)
    )
    return SimpleNamespace(app=app, state=SimpleNamespace(db=None))


def test_form_transaction_consumes_and_commits(db):
    request = make_request(db)
    token = issue_form_token(SECRET)
    with form_transaction(request, token) as s:
        s.add(FormToken(token="marker", used_at=utcnow()))
    with db.begin() as s:
        assert token_count(s) == 2
    with pytest.raises(FormTokenUsed):
        with form_transaction(make_request(db), token):
            pass


def test_form_transaction_without_token(db):
    for token in (None, ""):
        with pytest.raises(FormTokenInvalid):
            with form_transaction(make_request(db), token):
                pass


def test_form_transaction_rolls_back_with_the_token(db):
    request = make_request(db)
    token = issue_form_token(SECRET)
    with pytest.raises(ValidationError):
        with form_transaction(request, token):
            raise ValidationError("form.required", field="x")
    with db.begin() as s:
        assert token_count(s) == 0
    with form_transaction(make_request(db), token):
        pass
