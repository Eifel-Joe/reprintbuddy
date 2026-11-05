import tempfile

import pytest
import trustme
from cryptography.hazmat.primitives import serialization

from reprintbuddy.ops.tls import validate_tls_pair


def pair(name="localhost"):
    leaf = trustme.CA().issue_cert(name)
    return leaf.cert_chain_pems[0].bytes().decode(), leaf.private_key_pem.bytes().decode()


def test_valid_pair():
    cert, key = pair()
    validate_tls_pair(cert, key)


def test_mismatched_pair():
    cert_a, _ = pair("a.test")
    _, key_b = pair("b.test")
    with pytest.raises(ValueError, match="tls_invalid"):
        validate_tls_pair(cert_a, key_b)


def test_garbage():
    with pytest.raises(ValueError, match="tls_invalid"):
        validate_tls_pair("-----BEGIN CERTIFICATE-----x", "-----BEGIN PRIVATE KEY-----y")


def test_encrypted_key_rejected():
    cert, key = pair()
    loaded = serialization.load_pem_private_key(key.encode(), password=None)
    encrypted = loaded.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pw"),
    ).decode()
    with pytest.raises(ValueError, match="tls_encrypted"):
        validate_tls_pair(cert, encrypted)


def test_encrypted_legacy_key_rejected():
    """The older PEM format marks encryption in a header line."""
    cert, _ = pair()
    legacy = (
        "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\nAAAA\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    with pytest.raises(ValueError, match="tls_encrypted"):
        validate_tls_pair(cert, legacy)


def test_error_never_contains_the_input():
    cert, _ = pair()
    with pytest.raises(ValueError) as error:
        validate_tls_pair(cert, "-----BEGIN PRIVATE KEY-----SECRETMARK")
    assert "SECRETMARK" not in str(error.value)


def test_temp_files_removed(tmp_path, monkeypatch):
    folder = tmp_path / "t"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    cert, key = pair()
    validate_tls_pair(cert, key)
    with pytest.raises(ValueError):
        validate_tls_pair(cert, "-----BEGIN PRIVATE KEY-----x")
    assert list(folder.iterdir()) == []
