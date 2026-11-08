"""Password encryption of the full backup (Spec 11.6): Argon2id and AES-256-GCM.

Only recognised building blocks, nothing home-made:
- Argon2id (RFC 9106) turns the password into 64 bytes. The first 32 are the AES key,
  a SHA-256 of the other 32 is stored in the backup as a check value. It tells a wrong
  password apart from a damaged file and reveals nothing an attacker could not learn
  by trying passwords against the encrypted data anyway.
- AES-256-GCM (NIST SP 800-38D) encrypts and authenticates the database. The caller
  passes the manifest as associated data, so a changed manifest fails like changed data.

The parameters of a backup being read come from the file. Outside narrow limits they
are refused before Argon2 runs, so a crafted file cannot ask for gigabytes of memory.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
TAG_BYTES = 16


@dataclass(frozen=True)
class KdfParams:
    time_cost: int
    memory_cost: int  # KiB, as argon2 counts it
    parallelism: int


# RFC 9106, second recommended option ("low memory"): t=3, 64 MiB, p=4.
DEFAULT_KDF = KdfParams(time_cost=3, memory_cost=64 * 1024, parallelism=4)
# No more memory than this version asks for itself: two restores may run at once (the
# limiter that also serves logins), and a file must not decide how much RAM they take.
_LIMITS = {"time_cost": (1, 10), "memory_cost": (8 * 1024, 64 * 1024), "parallelism": (1, 8)}


class DecryptError(Exception):
    """The data does not open with this key: changed, damaged or a different key."""


def params_allowed(params: KdfParams) -> bool:
    for name, (low, high) in _LIMITS.items():
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return False
    return True


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def new_nonce() -> bytes:
    return secrets.token_bytes(NONCE_BYTES)


def derive_key(password: str, salt: bytes, params: KdfParams) -> tuple[bytes, bytes]:
    """Return (AES key, check value) for this password.

    The password is NFC-normalised first: the same word typed on another system
    (an umlaut as one or as two code points) must open the backup.
    """
    if not params_allowed(params):
        raise ValueError("key derivation parameters outside the allowed limits")
    if len(salt) != SALT_BYTES:
        raise ValueError("wrong salt length")
    raw = hash_secret_raw(
        secret=unicodedata.normalize("NFC", password).encode("utf-8"),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=2 * KEY_BYTES,
        type=Type.ID,
    )
    return raw[:KEY_BYTES], hashlib.sha256(raw[KEY_BYTES:]).digest()


def check_matches(candidate: bytes, stored: bytes) -> bool:
    return hmac.compare_digest(candidate, stored)


def seal(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
    """Encrypt and authenticate; the result is the ciphertext followed by the tag."""
    if len(nonce) != NONCE_BYTES:
        raise ValueError("wrong nonce length")
    return AESGCM(key).encrypt(nonce, data, aad)


class Sealer:
    """`seal` in blocks: the same bytes (ciphertext, then the tag), without the whole data in memory.

    `update(block)` returns the ciphertext of that block, `finish()` the tag. Used once, in that order."""

    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        if len(nonce) != NONCE_BYTES:
            raise ValueError("wrong nonce length")
        self._encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self._encryptor.authenticate_additional_data(aad)
        self._finished = False

    def update(self, data: bytes) -> bytes:
        if self._finished:
            raise ValueError("already finished")
        return self._encryptor.update(data)

    def finish(self) -> bytes:
        if self._finished:
            raise ValueError("already finished")
        self._finished = True
        return self._encryptor.finalize() + self._encryptor.tag


class Opener:
    """The counterpart of `Sealer`: `seal` read back in blocks, without the whole data in memory (v0.3.10 p125).

    `update(block)` takes the next bytes of the sealed payload (ciphertext, then the 16-byte tag) and returns
    the plaintext that is certain to be ciphertext; the last 16 bytes seen so far are held back, because the
    tag sits at the end and its length is not known before the data ends. `finish()` checks the tag and raises
    `DecryptError` if it does not fit.

    AES-GCM authenticates only at the end: everything `update` returned is UNVERIFIED until `finish()` has
    returned. A caller must keep it out of reach (a temporary file that is deleted on any error) until then.
    Used once, in that order."""

    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        if len(nonce) != NONCE_BYTES:
            raise ValueError("wrong nonce length")
        self._decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
        self._decryptor.authenticate_additional_data(aad)
        self._held = b""
        self._finished = False

    def update(self, data: bytes) -> bytes:
        if self._finished:
            raise ValueError("already finished")
        held = self._held + data
        if len(held) <= TAG_BYTES:
            self._held = held
            return b""
        self._held = held[-TAG_BYTES:]
        return self._decryptor.update(held[:-TAG_BYTES])

    def finish(self) -> None:
        if self._finished:
            raise ValueError("already finished")
        self._finished = True
        if len(self._held) != TAG_BYTES:
            raise DecryptError("not a sealed payload")
        try:
            self._decryptor.finalize_with_tag(self._held)
        except InvalidTag as error:
            raise DecryptError("the data does not open with this key") from error
