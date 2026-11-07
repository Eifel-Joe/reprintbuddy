"""Tripwire for B2 stage 2: the HTTP client must grow the queue methods of the protocol.

Until then `HttpBambuddyClient` offers only the reading methods, so it is not yet a
`BambuddyClient`. The strict xfail turns into a failure the moment stage 2 completes the
client: remove the mark then.
"""

import httpx
import pytest

from reprintbuddy.bambuddy.client import BambuddyClient, HttpBambuddyClient


@pytest.mark.xfail(strict=True, reason="queue methods come with B2 stage 2")
def test_http_client_implements_the_whole_protocol():
    http_client = HttpBambuddyClient("http://bambuddy.test", "k", lambda: httpx.AsyncClient())
    assert isinstance(http_client, BambuddyClient)
