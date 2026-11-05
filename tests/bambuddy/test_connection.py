import httpx
from conftest import REQUIRED, auth_me

from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS, ConnectionCheck, check_connection

URL = "http://bambuddy.test:8000"
ME = f"{URL}/api/v1/auth/me"


async def run(url=URL, key="bb_testkey"):
    async with httpx.AsyncClient() as client:
        return await check_connection(url, key, client)


def test_required_permissions_are_the_seven_from_the_spec():
    assert REQUIRED_PERMISSIONS == REQUIRED
    assert len(REQUIRED_PERMISSIONS) == 7


async def test_filaments_read_is_required(respx_mock):
    """Spec 9.1, 11.3 a: the mode of the spool management is read with it."""
    old_six = [p for p in REQUIRED if p != "filaments:read"]
    respx_mock.get(ME).respond(200, json=auth_me(permissions=old_six))
    result = await run()
    assert (result.ok, result.reason) == (False, "missing_permissions")
    assert result.missing == ("filaments:read",)


def test_labels_as_bambuddy_shows_them():
    from reprintbuddy.bambuddy.connection import EDITOR_LABELS, KEY_RIGHT_FOR

    assert EDITOR_LABELS["queue:start_unreviewed"] == "Print Without Review (else jobs wait for someone to start them)"
    assert EDITOR_LABELS["filaments:read"] == "Read Filaments"
    assert KEY_RIGHT_FOR["filaments:read"] == "status"  # at the key it is covered by "Read Status"


async def test_ok_with_all_permissions(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me())
    result = await run()
    assert result.ok is True
    assert result.reason == "ok"
    assert result.owner == "reprintbuddy"
    assert result.owner_is_admin is False
    assert result.missing == ()
    assert set(REQUIRED) <= result.granted


async def test_read_own_is_not_enough(respx_mock):
    permissions = ["library:read_own" if p == "library:read_all" else p for p in REQUIRED]
    respx_mock.get(ME).respond(200, json=auth_me(permissions=permissions))
    result = await run()
    assert result.ok is False
    assert result.reason == "missing_permissions"
    assert result.missing == ("library:read_all",)
    assert result.owner == "reprintbuddy"


async def test_legacy_read_names_accepted(respx_mock):
    # Spec 14: until the first live test shows the real names, the older names count.
    permissions = ["library:read" if p == "library:read_all" else p for p in REQUIRED]
    permissions = ["queue:read" if p == "queue:read_all" else p for p in permissions]
    respx_mock.get(ME).respond(200, json=auth_me(permissions=permissions))
    assert (await run()).ok is True


async def test_missing_start_unreviewed(respx_mock):
    permissions = [p for p in REQUIRED if p != "queue:start_unreviewed"]
    respx_mock.get(ME).respond(200, json=auth_me(permissions=permissions))
    result = await run()
    assert (result.ok, result.reason) == (False, "missing_permissions")
    assert result.missing == ("queue:start_unreviewed",)


async def test_missing_rights_are_listed_in_the_order_of_the_requirements(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(permissions=["printers:read"]))
    result = await run()
    assert result.missing == tuple(p for p in REQUIRED if p != "printers:read")


async def test_admin_owner_flagged_by_is_admin(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(username="boss", is_admin=True))
    result = await run()
    assert result.ok is True
    assert result.owner_is_admin is True


async def test_admin_owner_flagged_by_group(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(groups=("Administrators",)))
    assert (await run()).owner_is_admin is True


async def test_groups_may_be_plain_names(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(groups=()) | {"groups": ["Administrators"]})
    assert (await run()).owner_is_admin is True


async def test_unauthorized(respx_mock):
    respx_mock.get(ME).respond(401, json={"detail": "no"})
    assert await run() == ConnectionCheck(ok=False, reason="unauthorized")


async def test_forbidden(respx_mock):
    respx_mock.get(ME).respond(403, json={"detail": "no"})
    assert await run() == ConnectionCheck(ok=False, reason="forbidden")


async def test_unreachable(respx_mock):
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("refused"))
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_timeout(respx_mock):
    respx_mock.get(ME).mock(side_effect=httpx.ReadTimeout("slow"))
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_server_error(respx_mock):
    respx_mock.get(ME).respond(500, text="boom")
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_not_bambuddy_html(respx_mock):
    respx_mock.get(ME).respond(200, html="<html>router login</html>")
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_missing_fields(respx_mock):
    body = auth_me()
    del body["permissions"]
    respx_mock.get(ME).respond(200, json=body)
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_without_user_name(respx_mock):
    body = auth_me()
    del body["username"]
    respx_mock.get(ME).respond(200, json=body)
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_when_permissions_is_not_a_list(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(permissions="everything"))
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_when_permissions_hold_non_text(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(permissions=[1, None]))
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_json_but_not_an_object(respx_mock):
    respx_mock.get(ME).respond(200, json=[])
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_found_is_not_bambuddy(respx_mock):
    respx_mock.get(ME).respond(404, text="nope")
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_sends_key_header_to_auth_me(respx_mock):
    route = respx_mock.get(ME).respond(200, json=auth_me())
    await run(key="bb_headercheck")
    assert route.calls.last.request.headers["x-api-key"] == "bb_headercheck"
    assert route.calls.last.request.url.path == "/api/v1/auth/me"


async def test_trailing_slash_in_the_address_is_fine(respx_mock):
    route = respx_mock.get(ME).respond(200, json=auth_me())
    assert (await run(url=URL + "/")).ok is True
    assert route.called


async def test_uses_timeout_of_ten_seconds(respx_mock):
    route = respx_mock.get(ME).respond(200, json=auth_me())
    await run()
    assert route.calls.last.request.extensions["timeout"]["read"] == 10.0


async def test_invalid_url_is_unreachable():
    assert await run(url="http://") == ConnectionCheck(ok=False, reason="unreachable")


async def test_key_is_not_logged(respx_mock, caplog):
    caplog.set_level("DEBUG")
    respx_mock.get(ME).respond(401)
    await run(key="bb_LEAKCHECK_logs")
    assert "LEAKCHECK" not in caplog.text


def test_key_creation_rights_are_not_required():
    assert not any(p.startswith("api_keys:") or p == "settings:read" for p in REQUIRED_PERMISSIONS)


async def test_a_key_without_the_key_creation_rights_passes(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me(permissions=list(REQUIRED)))
    assert (await run()).ok is True


def test_key_creation_labels_are_a_fixed_table():
    from reprintbuddy.bambuddy.connection import KEY_CREATION_LABELS

    assert KEY_CREATION_LABELS == {
        "api_keys:read": "Read Api Keys",
        "api_keys:create": "Create Api Keys",
        "settings:read": "Read Settings",
    }
