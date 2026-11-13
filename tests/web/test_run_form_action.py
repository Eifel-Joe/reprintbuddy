"""`run_form_action` (web/forms.py) also carries the forms that show their input again (0.4.8, R9).

New over 0.4.7: `check` reads the input before the write transaction, so a rejected input is shown even
for a form sent twice (the one-time token is checked first thing inside the transaction); `once=False`
saves without a one-time token (editing a saved record); `done(result)` answers a success that is more
than one message; `duplicate_url` is where a repeat goes when the success address depends on the result.
"""

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from conftest import csrf_of
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.web.formtoken import issue_form_token
from reprintbuddy.web.forms import parse_int, run_form_action


def _refused(text, status, exc):
    return PlainTextResponse(f"refused {status}: {text}", status_code=status)


def _routes() -> APIRouter:
    router = APIRouter()

    @router.post("/test-form/checked")
    async def checked(request: Request):
        form = await request.form()
        return await run_form_action(
            request,
            check=lambda: parse_int(form.get("n"), field="orders.field.qty", min=1, max=9),
            work=lambda tx, n: {"n": n * 2},
            done=lambda result: PlainTextResponse(f"saved {result['n']}"),
            duplicate_url="/test-form/list",
            on_error=_refused,
        )

    @router.post("/test-form/edit")
    async def edit(request: Request):
        return await run_form_action(
            request, once=False, work=lambda tx: "edited",
            done=lambda result: PlainTextResponse(result), on_error=_refused,
        )

    @router.post("/test-form/async-done")
    async def async_done(request: Request):
        async def answer(result):
            return PlainTextResponse(f"async {result}")

        return await run_form_action(
            request, work=lambda tx: 7, done=answer, duplicate_url="/test-form/list", on_error=_refused
        )

    @router.post("/test-form/plain")
    async def plain(request: Request):
        return await run_form_action(
            request, work=lambda tx: {"name": "x"}, success_url="/parts", flash_key="parts.created",
            on_error=_refused,
        )

    return router


@pytest.fixture
def form_client(ready_client):
    ready_client.app.include_router(_routes())
    return ready_client


def _fields(client, **extra):
    data = {
        "csrf_token": csrf_of(client, "/parts/new"),
        "form_token": issue_form_token(client.app.state.session_secret),
    }
    data.update(extra)
    return data


def test_check_reads_the_input_and_work_gets_it(form_client):
    response = form_client.post("/test-form/checked", data=_fields(form_client, n="3"))
    assert (response.status_code, response.text) == (200, "saved 6")


def test_a_rejected_input_is_shown_before_the_token_is_looked_at(form_client):
    data = _fields(form_client, n="3")
    assert form_client.post("/test-form/checked", data=data).text == "saved 6"
    # The same form again, now with a bad number: the input error, not "already saved".
    again = form_client.post("/test-form/checked", data={**data, "n": "x"}, follow_redirects=False)
    assert again.status_code == 200 and again.text.startswith("refused 200: ")


def test_a_repeat_goes_to_the_duplicate_address(form_client):
    data = _fields(form_client, n="3")
    form_client.post("/test-form/checked", data=data)
    again = form_client.post("/test-form/checked", data=data, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/test-form/list"


def test_an_expired_or_missing_token_is_refused_with_400(form_client):
    data = _fields(form_client, n="3")
    for token in ("", "forged"):
        response = form_client.post("/test-form/checked", data={**data, "form_token": token})
        assert response.status_code == 400 and response.text.startswith("refused 400: ")


def test_editing_saves_without_a_one_time_token(form_client):
    data = {"csrf_token": csrf_of(form_client, "/parts/new")}
    for _ in range(2):  # no token, so no "already saved" either
        response = form_client.post("/test-form/edit", data=data)
        assert (response.status_code, response.text) == (200, "edited")


def test_done_may_be_a_coroutine(form_client):
    assert form_client.post("/test-form/async-done", data=_fields(form_client)).text == "async 7"


def test_the_form_without_the_new_parameters_works_as_before(form_client):
    data = _fields(form_client)
    first = form_client.post("/test-form/plain", data=data, follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"] == "/parts"
    again = form_client.post("/test-form/plain", data=data, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/parts"


def test_a_validation_error_in_work_is_refused_too(form_client):
    """What `work` refuses (the domain) is shown like what `check` refused (the input)."""

    async def route(request: Request):
        def work(tx):
            raise ValidationError("form.required", field="orders.field.qty")

        return await run_form_action(
            request, work=work, done=PlainTextResponse, duplicate_url="/test-form/list", on_error=_refused
        )

    form_client.app.add_api_route("/test-form/refused", route, methods=["POST"])
    response = form_client.post("/test-form/refused", data=_fields(form_client))
    assert response.status_code == 200 and response.text.startswith("refused 200: ")


def test_a_one_time_form_needs_somewhere_to_send_a_repeat(form_client):
    """With `done` and neither `duplicate_url` nor `success_url`, a repeat would go to "None"."""

    async def route(request: Request):
        return await run_form_action(request, work=lambda tx: 1, done=PlainTextResponse, on_error=_refused)

    form_client.app.add_api_route("/test-form/nowhere", route, methods=["POST"])
    with pytest.raises(TypeError, match="duplicate_url"):
        form_client.post("/test-form/nowhere", data=_fields(form_client))
