import httpx

from lagerbuddy.bambuddy.connection import ConnectionCheck, check_connection

URL = "http://bambuddy.test:8000"
QUEUE = f"{URL}/api/v1/queue/"


async def run(url=URL, key="bb_testkey"):
    async with httpx.AsyncClient() as client:
        return await check_connection(url, key, client)


async def test_ok(respx_mock):
    respx_mock.get(QUEUE).respond(200, json=[])
    assert await run() == ConnectionCheck(ok=True, reason="ok")


async def test_ok_with_items(respx_mock):
    respx_mock.get(QUEUE).respond(200, json=[{"id": 1}])
    assert (await run()).ok is True


async def test_unauthorized(respx_mock):
    respx_mock.get(QUEUE).respond(401, json={"detail": "no"})
    assert await run() == ConnectionCheck(ok=False, reason="unauthorized")


async def test_forbidden(respx_mock):
    respx_mock.get(QUEUE).respond(403, json={"detail": "no"})
    assert await run() == ConnectionCheck(ok=False, reason="forbidden")


async def test_unreachable(respx_mock):
    respx_mock.get(QUEUE).mock(side_effect=httpx.ConnectError("refused"))
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_timeout(respx_mock):
    respx_mock.get(QUEUE).mock(side_effect=httpx.ReadTimeout("slow"))
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_server_error(respx_mock):
    respx_mock.get(QUEUE).respond(500, text="boom")
    assert await run() == ConnectionCheck(ok=False, reason="unreachable")


async def test_not_bambuddy_html(respx_mock):
    respx_mock.get(QUEUE).respond(200, html="<html>router login</html>")
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_bambuddy_json_but_not_a_list(respx_mock):
    respx_mock.get(QUEUE).respond(200, json={"hello": "world"})
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_not_found_is_not_bambuddy(respx_mock):
    respx_mock.get(QUEUE).respond(404, text="nope")
    assert await run() == ConnectionCheck(ok=False, reason="not_bambuddy")


async def test_sends_key_header(respx_mock):
    route = respx_mock.get(QUEUE).respond(200, json=[])
    await run(key="bb_headercheck")
    assert route.calls.last.request.headers["x-api-key"] == "bb_headercheck"


async def test_uses_timeout_of_ten_seconds(respx_mock):
    route = respx_mock.get(QUEUE).respond(200, json=[])
    await run()
    assert route.calls.last.request.extensions["timeout"]["read"] == 10.0


async def test_invalid_url_is_unreachable():
    assert await run(url="http://") == ConnectionCheck(ok=False, reason="unreachable")


async def test_key_is_not_logged(respx_mock, caplog):
    caplog.set_level("DEBUG")
    respx_mock.get(QUEUE).respond(401)
    await run(key="bb_LEAKCHECK_logs")
    assert "LEAKCHECK" not in caplog.text
