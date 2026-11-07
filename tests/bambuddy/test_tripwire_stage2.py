"""The HTTP client implements the whole protocol (B2 stage 2 completed it).

Kept as a plain test: if a protocol method is added without the client following,
this fails.
"""

import httpx

from reprintbuddy.bambuddy.client import BambuddyClient, HttpBambuddyClient


def test_http_client_implements_the_whole_protocol():
    http_client = HttpBambuddyClient("http://bambuddy.test", "k", lambda: httpx.AsyncClient())
    assert isinstance(http_client, BambuddyClient)


def test_protocol_has_no_batch_calls():
    """Spec 7.6 (v0.3.5): single entries; the unclear search reads the whole queue."""
    names = {n for n in dir(BambuddyClient) if not n.startswith("_")}
    assert "ensure_batch" not in names and "find_items_by_batch" not in names
    assert "list_all_items" in names and "enqueue" in names
