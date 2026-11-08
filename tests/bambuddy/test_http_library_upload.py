"""Uploading one print file through `HttpBambuddyClient` (Spec 5.1a, v0.3.9 p20).

Bambuddy `api/routes/library.py:2509-2677` (tag v1.3.1b1-daily.20261107): `POST /library/files`,
multipart field `file`, the target folder only as query `folder_id`, answer `FileUploadResponse`
(`schemas/library.py:328-337`).
"""

import io
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import pytest
from fixtures import API, KEY, client

from reprintbuddy.bambuddy.client import (
    UPLOAD_TIMEOUT_SECONDS,
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
)
from reprintbuddy.bambuddy.types import UploadedFile

ANSWER = {
    "id": 42,
    "filename": "Template A.gcode.3mf",
    "file_type": "gcode.3mf",
    "file_size": 11,
    "thumbnail_path": "thumbnails/abc.png",
    "duplicate_of": None,
    "metadata": {"print_name": "x"},
}
CONTENT = b"PK\x03\x04-bytes"


def _parts(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    """name -> (filename, content) of a multipart request."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    return {
        part.get_param("name", header="content-disposition"): (part.get_filename(), part.get_payload(decode=True))
        for part in message.iter_parts()
    }


async def test_upload_into_a_folder(respx_mock):
    route = respx_mock.post(f"{API}/library/files").respond(200, json=ANSWER)
    result = await client().upload_file(7, "Template A.gcode.3mf", io.BytesIO(CONTENT))
    assert result == UploadedFile(id=42, filename="Template A.gcode.3mf", sliced=True, duplicate_of=None)
    request = route.calls.last.request
    assert request.url.params.multi_items() == [("folder_id", "7")]
    assert request.headers["x-api-key"] == KEY
    assert _parts(request) == {"file": ("Template A.gcode.3mf", CONTENT)}


async def test_upload_to_the_top_level_sends_no_folder(respx_mock):
    route = respx_mock.post(f"{API}/library/files").respond(200, json=ANSWER)
    await client().upload_file(None, "a.3mf", io.BytesIO(CONTENT))
    assert route.calls.last.request.url.params.multi_items() == []


async def test_a_name_with_umlauts_arrives_unchanged(respx_mock):
    route = respx_mock.post(f"{API}/library/files").respond(200, json={**ANSWER, "filename": "Größe ä.3mf"})
    await client().upload_file(1, "Größe ä.3mf", io.BytesIO(CONTENT))
    assert _parts(route.calls.last.request)["file"][0] == "Größe ä.3mf"


async def test_the_bytes_sent_are_the_file_from_its_start(respx_mock):
    route = respx_mock.post(f"{API}/library/files").respond(200, json=ANSWER)
    content = io.BytesIO(b"PK\x03\x04" + bytes(range(256)) * 50)
    content.seek(100)  # a check before may have read part of it
    await client().upload_file(1, "a.3mf", content)
    assert _parts(route.calls.last.request)["file"][1] == content.getvalue()


async def test_upload_uses_its_long_timeout_and_no_redirect(respx_mock):
    seen = {}

    class Recording(httpx.AsyncClient):
        async def request(self, *args, **kwargs):
            seen.update(kwargs)
            return await super().request(*args, **kwargs)

    respx_mock.post(f"{API}/library/files").respond(200, json=ANSWER)
    await client(lambda: Recording()).upload_file(1, "a.3mf", io.BytesIO(CONTENT))
    assert seen["timeout"] == UPLOAD_TIMEOUT_SECONDS >= 300
    assert seen["follow_redirects"] is False


async def test_duplicate_and_unsliced_are_reported(respx_mock):
    respx_mock.post(f"{API}/library/files").respond(200, json={**ANSWER, "file_type": "3mf", "duplicate_of": 5})
    result = await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))
    assert (result.sliced, result.duplicate_of) == (False, 5)


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413])
async def test_refusals_carry_status_and_detail(respx_mock, status):
    respx_mock.post(f"{API}/library/files").respond(status, json={"detail": "API key does not have 'can_manage_library' permission"})
    with pytest.raises(BambuddyRejected) as caught:
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))
    assert caught.value.status == status
    assert "can_manage_library" in caught.value.detail


async def test_server_error_and_network_are_unavailable(respx_mock):
    respx_mock.post(f"{API}/library/files").respond(502)
    with pytest.raises(BambuddyUnavailable):
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))
    respx_mock.post(f"{API}/library/files").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))


@pytest.mark.parametrize(
    "answer",
    [
        [ANSWER],
        {**ANSWER, "id": "42"},
        {**ANSWER, "id": True},
        {k: v for k, v in ANSWER.items() if k != "filename"},
        {**ANSWER, "duplicate_of": "5"},
        {**ANSWER, "file_type": 3},
    ],
)
async def test_an_answer_of_the_wrong_shape_is_unexpected(respx_mock, answer):
    respx_mock.post(f"{API}/library/files").respond(200, json=answer)
    with pytest.raises(BambuddyUnexpected):
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))


async def test_a_redirect_is_unexpected(respx_mock):
    respx_mock.post(f"{API}/library/files").respond(307, headers={"location": "http://elsewhere/"})
    with pytest.raises(BambuddyUnexpected):
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))


async def test_the_key_is_never_in_an_error(respx_mock):
    respx_mock.post(f"{API}/library/files").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BambuddyUnavailable) as caught:
        await client().upload_file(1, "a.3mf", io.BytesIO(CONTENT))
    assert KEY not in str(caught.value)
