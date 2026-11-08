"""Password encryption of the full backup: Argon2id for the key, AES-256-GCM for the data."""

import pytest

from reprintbuddy.ops import backupcrypto as bc

# Small parameters keep the tests fast; the format and the checks are the same.
FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
SALT = bytes(range(16))
NONCE = bytes(range(12))
AAD = b'{"format": "test"}'


def test_defaults_follow_rfc_9106_low_memory_profile():
    assert bc.DEFAULT_KDF == bc.KdfParams(time_cost=3, memory_cost=64 * 1024, parallelism=4)
    assert bc.params_allowed(bc.DEFAULT_KDF)


def test_key_and_check_are_deterministic_and_separate():
    key, check = bc.derive_key("geheim-123", SALT, FAST)
    assert len(key) == 32 and len(check) == 32
    assert (key, check) == bc.derive_key("geheim-123", SALT, FAST)
    assert key != check
    assert bc.derive_key("geheim-124", SALT, FAST)[0] != key
    assert bc.derive_key("geheim-123", bytes(16), FAST)[0] != key


def test_password_is_unicode_normalised():
    composed = "Käse-1234"  # ä as one code point
    decomposed = "Käse-1234"  # a + combining diaeresis (as some systems type it)
    assert bc.derive_key(composed, SALT, FAST) == bc.derive_key(decomposed, SALT, FAST)


def test_check_matches_only_the_right_password():
    _, check = bc.derive_key("richtig-123", SALT, FAST)
    assert bc.check_matches(bc.derive_key("richtig-123", SALT, FAST)[1], check)
    assert not bc.check_matches(bc.derive_key("falsch-1234", SALT, FAST)[1], check)


def test_seal_and_open_roundtrip():
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    sealed = bc.seal(key, NONCE, b"database bytes", AAD)
    assert b"database bytes" not in sealed
    assert len(sealed) == len(b"database bytes") + 16  # the GCM tag
    assert bc.open_sealed(key, NONCE, sealed, AAD) == b"database bytes"


@pytest.mark.parametrize(
    "change",
    [
        lambda key, nonce, data, aad: (key, nonce, data[:-1] + bytes([data[-1] ^ 1]), aad),
        lambda key, nonce, data, aad: (key, nonce, bytes([data[0] ^ 1]) + data[1:], aad),
        lambda key, nonce, data, aad: (key, nonce, data, aad + b" "),
        lambda key, nonce, data, aad: (key, bytes(12), data, aad),
        lambda key, nonce, data, aad: (bytes(32), nonce, data, aad),
        lambda key, nonce, data, aad: (key, nonce, data[:10], aad),
        lambda key, nonce, data, aad: (key, nonce, b"", aad),
    ],
)
def test_any_change_is_detected(change):
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    sealed = bc.seal(key, NONCE, b"database bytes", AAD)
    with pytest.raises(bc.DecryptError):
        bc.open_sealed(*change(key, NONCE, sealed, AAD))


@pytest.mark.parametrize(
    "params",
    [
        bc.KdfParams(time_cost=0, memory_cost=64 * 1024, parallelism=4),
        bc.KdfParams(time_cost=11, memory_cost=64 * 1024, parallelism=4),
        bc.KdfParams(time_cost=3, memory_cost=4 * 1024 * 1024, parallelism=4),  # 4 GiB
        bc.KdfParams(time_cost=3, memory_cost=1024, parallelism=4),
        bc.KdfParams(time_cost=3, memory_cost=64 * 1024, parallelism=0),
        bc.KdfParams(time_cost=3, memory_cost=64 * 1024, parallelism=9),
        bc.KdfParams(time_cost=True, memory_cost=64 * 1024, parallelism=4),
        bc.KdfParams(time_cost="3", memory_cost=64 * 1024, parallelism=4),
    ],
)
def test_params_outside_the_limits_are_refused(params):
    assert not bc.params_allowed(params)
    with pytest.raises(ValueError):
        bc.derive_key("geheim-123", SALT, params)


def test_salt_and_nonce_sizes_are_enforced():
    with pytest.raises(ValueError):
        bc.derive_key("geheim-123", b"short", FAST)
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    with pytest.raises(ValueError):
        bc.seal(key, b"short", b"x", AAD)


def test_fresh_salt_and_nonce_are_random():
    assert len(bc.new_salt()) == 16 and len(bc.new_nonce()) == 12
    assert bc.new_salt() != bc.new_salt()
    assert bc.new_nonce() != bc.new_nonce()


# --- sealing in blocks (v0.3.9 p102) ---------------------------------------------------------


@pytest.mark.parametrize("sizes", [[0], [1], [5, 0, 11, 1000], [100_000]])
def test_sealing_in_blocks_gives_what_sealing_at_once_gives(sizes):
    key, nonce, aad = b"k" * bc.KEY_BYTES, b"n" * bc.NONCE_BYTES, b"manifest bytes"
    blocks = [bytes([i + 1]) * size for i, size in enumerate(sizes)]
    sealer = bc.Sealer(key, nonce, aad)
    pieces = [sealer.update(block) for block in blocks]
    pieces.append(sealer.finish())
    assert b"".join(pieces) == bc.seal(key, nonce, b"".join(blocks), aad)


def test_a_sealer_cannot_be_used_after_finish():
    sealer = bc.Sealer(b"k" * bc.KEY_BYTES, b"n" * bc.NONCE_BYTES, b"")
    sealer.finish()
    with pytest.raises(ValueError):
        sealer.update(b"x")
    with pytest.raises(ValueError):
        sealer.finish()


def test_a_sealer_checks_the_nonce_length():
    with pytest.raises(ValueError):
        bc.Sealer(b"k" * bc.KEY_BYTES, b"short", b"")


# --- Opener: open_sealed in blocks (v0.3.10 p125) ---------------------------------------------


def _opened(key, nonce, sealed, aad, size):
    opener = bc.Opener(key, nonce, aad)
    parts = [opener.update(sealed[i : i + size]) for i in range(0, len(sealed), size)]
    opener.finish()
    return b"".join(parts)


@pytest.mark.parametrize("size", [1, 7, 15, 16, 17, 33, 1000, 10_000])
def test_opener_gives_what_open_sealed_gives_whatever_the_block_size(size):
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    data = bytes(range(256)) * 20
    sealed = bc.seal(key, NONCE, data, AAD)
    assert _opened(key, NONCE, sealed, AAD, size) == data == bc.open_sealed(key, NONCE, sealed, AAD)


def test_opener_opens_an_empty_payload():
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    sealed = bc.seal(key, NONCE, b"", AAD)
    assert _opened(key, NONCE, sealed, AAD, 4) == b""


def test_opener_holds_back_the_tag():
    """The last 16 bytes are the tag, not data: nothing of them may come out as plaintext."""
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    sealed = bc.seal(key, NONCE, b"x" * 100, AAD)
    opener = bc.Opener(key, NONCE, AAD)
    assert len(opener.update(sealed)) == 100


@pytest.mark.parametrize(
    "change",
    [
        lambda key, nonce, data, aad: (key, nonce, data[:-1] + bytes([data[-1] ^ 1]), aad),
        lambda key, nonce, data, aad: (key, nonce, bytes([data[0] ^ 1]) + data[1:], aad),
        lambda key, nonce, data, aad: (key, nonce, data, aad + b" "),
        lambda key, nonce, data, aad: (key, bytes(12), data, aad),
        lambda key, nonce, data, aad: (bytes(32), nonce, data, aad),
        lambda key, nonce, data, aad: (key, nonce, data[:10], aad),
        lambda key, nonce, data, aad: (key, nonce, data[:-1], aad),
        lambda key, nonce, data, aad: (key, nonce, data + b"\0", aad),
        lambda key, nonce, data, aad: (key, nonce, b"", aad),
    ],
)
def test_opener_detects_any_change_at_the_end(change):
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    sealed = bc.seal(key, NONCE, b"database bytes", AAD)
    key, nonce, data, aad = change(key, NONCE, sealed, AAD)
    opener = bc.Opener(key, nonce, aad)
    opener.update(data)
    with pytest.raises(bc.DecryptError):
        opener.finish()


def test_opener_is_used_once():
    key, _ = bc.derive_key("geheim-123", SALT, FAST)
    opener = bc.Opener(key, NONCE, AAD)
    opener.update(bc.seal(key, NONCE, b"abc", AAD))
    opener.finish()
    with pytest.raises(ValueError):
        opener.update(b"more")
    with pytest.raises(ValueError):
        opener.finish()


def test_opener_refuses_a_wrong_nonce_length():
    with pytest.raises(ValueError):
        bc.Opener(bytes(32), b"short", AAD)
