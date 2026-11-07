"""The HTTP client implements the whole protocol (B2 stage 2 completed it).

Kept as a plain test: if a protocol method is added without the client following,
this fails.
"""

import httpx

from reprintbuddy.bambuddy.client import BambuddyClient, HttpBambuddyClient


def test_http_client_implements_the_whole_protocol():
    http_client = HttpBambuddyClient("http://bambuddy.test", "k", lambda: httpx.AsyncClient())
    assert isinstance(http_client, BambuddyClient)
