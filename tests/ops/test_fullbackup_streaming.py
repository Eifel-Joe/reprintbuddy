"""The full backup is written step by step, and its format did not change (v0.3.9 p102).

`create_backup` used to hold the database, its packed form and its sealed form in memory at the same
time (about three times the database). It now reads, packs and seals in blocks and writes straight into
the file. The file format stays: a backup made by an older version opens, and a new one equals what the
old way of writing made, byte for byte (same salt and nonce given).
"""

import contextlib
import hashlib
import secrets
import sqlite3
import tracemalloc
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, 5, tzinfo=UTC)
FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
FIXTURE_PASSWORD = "fixture-password-1"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "data" / "reprintbuddy.db"
    path.parent.mkdir()
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    maker = make_sessionmaker(make_engine(path))
    with maker() as session:
        st.set_setting(session, "queue_cap", 7)
        st.ensure_session_secret(session)
    return path


def legacy_create_backup(db_path: Path, *, work_dir: Path, password: str | None, now: datetime) -> Path:
    """The way of writing before v0.3.9 (everything in memory), kept as the reference of the format."""
    token = secrets.token_hex(8)
    snapshot = work_dir / f"snapshot-{token}.db"
    try:
        fb._snapshot(db_path, snapshot)
        revision = fb._revision(snapshot)
        data = snapshot.read_bytes()
    finally:
        fb.remove_database_files(snapshot)
    manifest = {
        "format": fb.FORMAT,
        "format_version": fb.FORMAT_VERSION,
        "app_version": fb.__version__,
        "schema_revision": revision,
        "created_at": now.isoformat(),
        "encrypted": bool(password),
    }
    if password:
        salt, nonce = bc.new_salt(), bc.new_nonce()
        key, check = bc.derive_key(password, salt, fb.KDF)
        manifest["encryption"] = {
            "cipher": fb.CIPHER,
            "kdf": fb.KDF_NAME,
            "time_cost": fb.KDF.time_cost,
            "memory_cost": fb.KDF.memory_cost,
            "parallelism": fb.KDF.parallelism,
            "salt": fb._b64(salt),
            "nonce": fb._b64(nonce),
            "check": fb._b64(check),
        }
        raw = fb._dump(manifest)
        member, payload, method = fb.SEALED, bc.seal(key, nonce, zlib.compress(data, 6), raw), zipfile.ZIP_STORED
    else:
        manifest["database"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        raw = fb._dump(manifest)
        member, payload, method = fb.PLAIN, data, zipfile.ZIP_DEFLATED
    stamp = now.timetuple()[:6]
    path = work_dir / f"legacy-{token}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo(fb.MANIFEST, stamp), raw, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(zipfile.ZipInfo(member, stamp), payload, compress_type=method)
    return path


def tables(path: Path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    finally:
        con.close()


# --- a backup in today's format still opens -------------------------------------------------------


@pytest.mark.parametrize(("name", "password"), [("backup-v1-plain.zip", None), ("backup-v1-encrypted.zip", FIXTURE_PASSWORD)])
def test_a_backup_made_before_v039_opens(name, password, work):
    """Both fixtures were written by v0.3.8's `create_backup` (kept as they are)."""
    path = FIXTURES / name
    manifest = fb.read_manifest(path)
    assert manifest.encrypted is (password is not None)
    target = work / "candidate.db"
    fb.extract_database(path, manifest, password, target)
    fb.verify_database(target, manifest, work_dir=work)
    assert ("queue_cap", "7") in tables(target)


# --- the new way writes what the old way wrote --------------------------------------------------


@pytest.mark.parametrize("password", [None, "sicher-genug-1"])
def test_the_file_equals_what_the_old_way_of_writing_made(live, work, monkeypatch, password):
    monkeypatch.setattr(bc, "new_salt", lambda: b"S" * bc.SALT_BYTES)
    monkeypatch.setattr(bc, "new_nonce", lambda: b"N" * bc.NONCE_BYTES)
    old = legacy_create_backup(live, work_dir=work, password=password, now=NOW)
    new = fb.create_backup(live, work_dir=work, password=password, now=NOW)
    assert new.path.read_bytes() == old.read_bytes()


# --- memory ---------------------------------------------------------------------------------------

MiB = 1024 * 1024


@pytest.fixture
def big_live(live):
    """A database of about 24 MiB that does not pack well (random rows)."""
    con = sqlite3.connect(live)
    try:
        con.execute("CREATE TABLE padding (id INTEGER PRIMARY KEY, blob BLOB NOT NULL)")
        con.executemany("INSERT INTO padding (blob) VALUES (?)", ((secrets.token_bytes(64 * 1024),) for _ in range(384)))
        con.commit()
    finally:
        con.close()
    assert live.stat().st_size > 24 * MiB
    return live


@pytest.mark.parametrize("password", [None, "sicher-genug-1"])
def test_writing_needs_a_few_blocks_not_a_multiple_of_the_database(big_live, work, password):
    tracemalloc.start()
    try:
        created = fb.create_backup(big_live, work_dir=work, password=password, now=NOW)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert created.path.stat().st_size > 20 * MiB
    assert peak < 8 * MiB, f"peak {peak / MiB:.1f} MiB for a {big_live.stat().st_size / MiB:.0f} MiB database"
    # and the result still reads back
    manifest = fb.read_manifest(created.path)
    target = work / "candidate.db"
    fb.extract_database(created.path, manifest, password, target)
    assert target.stat().st_size > 24 * MiB


def test_a_database_over_the_limit_is_refused_before_it_is_packed(live, work, monkeypatch):
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 1024)
    with pytest.raises(fb.TooLarge):
        fb.create_backup(live, work_dir=work, password=None, now=NOW)
    assert list(work.iterdir()) == []  # nothing is left behind


@pytest.mark.parametrize("password", [None, "sicher-genug-1"])
def test_the_file_equals_the_old_one_also_across_block_boundaries(big_live, work, monkeypatch, password):
    """A database of 24 MiB is read in 24 blocks: packing and sealing across the block ends gives the same bytes."""
    monkeypatch.setattr(bc, "new_salt", lambda: b"S" * bc.SALT_BYTES)
    monkeypatch.setattr(bc, "new_nonce", lambda: b"N" * bc.NONCE_BYTES)
    old = legacy_create_backup(big_live, work_dir=work, password=password, now=NOW)
    old_bytes = old.read_bytes()
    old.unlink()
    new = fb.create_backup(big_live, work_dir=work, password=password, now=NOW)
    assert new.path.read_bytes() == old_bytes


@pytest.mark.parametrize("password", [None, "sicher-genug-1"])
def test_a_failure_in_the_middle_of_writing_leaves_nothing_behind(live, work, monkeypatch, password):
    """The snapshot and the half-written file are gone, and the snapshot is closed first (on Windows an open
    file cannot be removed)."""
    real = fb._write_member
    calls = []

    def failing(archive, info, blocks):
        calls.append(info.filename)
        if info.filename == fb.MANIFEST:
            return real(archive, info, blocks)
        next(iter(blocks))  # the first block of the database has been read
        raise OSError("disk full")

    monkeypatch.setattr(fb, "_write_member", failing)
    with pytest.raises(OSError, match="disk full"):
        fb.create_backup(live, work_dir=work, password=password, now=NOW)
    assert len(calls) == 2
    assert list(work.iterdir()) == []


# --- reading an encrypted backup in blocks (v0.3.10 p125) -----------------------------------------


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def _rebuilt(path: Path, out: Path, *, manifest: bytes | None = None, sealed: bytes | None = None) -> Path:
    """The same backup with another manifest or sealed member (the ZIP checksums are made fresh, so
    only the encryption can tell)."""
    content = _members(path)
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(fb.MANIFEST, content[fb.MANIFEST] if manifest is None else manifest, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(fb.SEALED, content[fb.SEALED] if sealed is None else sealed, compress_type=zipfile.ZIP_STORED)
    return out


def _flip(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 1]) + data[index + 1 :]


@pytest.fixture
def sealed_small(live, work):
    created = fb.create_backup(live, work_dir=work, password="sicher-genug-1", now=NOW)
    return created.path


def test_reading_needs_a_few_blocks_not_a_multiple_of_the_database(big_live, work):
    created = fb.create_backup(big_live, work_dir=work, password="sicher-genug-1", now=NOW)
    manifest = fb.read_manifest(created.path)
    target = work / "candidate.db"
    tracemalloc.start()
    try:
        fb.extract_database(created.path, manifest, "sicher-genug-1", target)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert target.stat().st_size > 24 * MiB
    assert peak < 8 * MiB, f"peak {peak / MiB:.1f} MiB for a {target.stat().st_size / MiB:.0f} MiB database"
    con = sqlite3.connect(target)
    try:
        assert con.execute("SELECT count(*) FROM padding").fetchone() == (384,)  # the content came out whole
    finally:
        con.close()


def _tampered_variants(sealed: bytes):
    middle = len(sealed) // 2
    return {
        "first byte": _flip(sealed, 0),
        "middle": _flip(sealed, middle),
        "last byte of the tag": _flip(sealed, len(sealed) - 1),
        "first byte of the tag": _flip(sealed, len(sealed) - bc.TAG_BYTES),
        "tag cut short": sealed[:-1],
        "one byte appended": sealed + b"\0",
        "tag removed": sealed[: -bc.TAG_BYTES],
    }


@pytest.mark.parametrize(
    "variant",
    ["first byte", "middle", "last byte of the tag", "first byte of the tag", "tag cut short", "one byte appended", "tag removed"],
)
def test_a_changed_byte_is_refused_and_leaves_no_partial_result(sealed_small, work, variant):
    content = _members(sealed_small)
    path = _rebuilt(sealed_small, work / "changed.zip", sealed=_tampered_variants(content[fb.SEALED])[variant])
    target = work / "candidate.db"
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
    assert not target.exists()
    assert sorted(p.name for p in work.iterdir()) == sorted(["changed.zip", sealed_small.name])  # no half result


def test_only_the_tag_is_wrong_the_whole_database_was_written_and_is_still_removed(sealed_small, work, monkeypatch):
    """The flipped tag byte leaves the packed data intact: it unpacks completely and the file exists in full
    when the check fails. It must be gone afterwards - nothing counts before the tag is checked."""
    content = _members(sealed_small)
    path = _rebuilt(sealed_small, work / "changed.zip", sealed=_flip(content[fb.SEALED], len(content[fb.SEALED]) - 1))
    target = work / "candidate.db"
    seen = {}
    real = bc.Opener.finish

    def spy(self):
        seen["size_before_the_check"] = target.stat().st_size if target.exists() else None
        return real(self)

    monkeypatch.setattr(bc.Opener, "finish", spy)
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
    assert seen["size_before_the_check"], "the test no longer shows what it claims: nothing was written before the check"
    assert not target.exists()


def test_a_changed_manifest_is_refused(sealed_small, work):
    content = _members(sealed_small)
    changed = content[fb.MANIFEST].replace(b"2026-11-07", b"2026-11-08")
    assert changed != content[fb.MANIFEST]
    path = _rebuilt(sealed_small, work / "changed.zip", manifest=changed)
    target = work / "candidate.db"
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
    assert not target.exists()


def _sealed_with(live, work, packed: bytes) -> Path:
    """A backup whose sealed content is `packed` (valid encryption, any content), made by hand."""
    base = fb.create_backup(live, work_dir=work, password="sicher-genug-1", now=NOW).path
    manifest = fb.read_manifest(base)
    sealing = manifest.sealing
    key, _ = bc.derive_key("sicher-genug-1", sealing.salt, sealing.params)
    sealed = bc.seal(key, sealing.nonce, packed, manifest.raw)
    return _rebuilt(base, work / "made.zip", sealed=sealed)


def test_a_zip_bomb_inside_valid_encryption_stops_at_the_limit(live, work, monkeypatch):
    """Authentic, but 64 MiB of zeros packs into a few kilobytes: unpacking must stop at the limit, not grow."""
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 2 * MiB)
    packed = zlib.compress(b"\0" * (64 * MiB), 9)
    assert len(packed) < 100 * 1024
    path = _sealed_with(live, work, packed)
    target = work / "candidate.db"
    tracemalloc.start()
    try:
        with pytest.raises(fb.TooLarge):
            fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * MiB, f"peak {peak / MiB:.1f} MiB"
    assert not target.exists()


def test_a_bomb_with_a_wrong_tag_is_still_stopped_at_the_limit(live, work, monkeypatch):
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 2 * MiB)
    path = _sealed_with(live, work, zlib.compress(b"\0" * (64 * MiB), 9))
    sealed = _members(path)[fb.SEALED]
    path = _rebuilt(path, work / "changed.zip", sealed=_flip(sealed, len(sealed) - 1))
    target = work / "candidate.db"
    with pytest.raises(fb.BackupError):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
    assert not target.exists()


@pytest.mark.parametrize(
    "packed",
    [b"not zlib at all", zlib.compress(b"abc" * 1000)[:-6], zlib.compress(b"abc" * 1000) + b"trailing"],
    ids=["no zlib", "cut short", "trailing bytes"],
)
def test_valid_encryption_around_unusable_data_is_refused_and_removed(live, work, packed):
    path = _sealed_with(live, work, packed)
    target = work / "candidate.db"
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", target)
    assert not target.exists()


def test_the_wrong_password_still_costs_no_reading(sealed_small, work, monkeypatch):
    manifest = fb.read_manifest(sealed_small)

    def forbidden(*args, **kwargs):
        raise AssertionError("the sealed member must not be read before the password fits")

    monkeypatch.setattr(fb, "_copy_member", forbidden)
    target = work / "candidate.db"
    with pytest.raises(fb.WrongPassword):
        fb.extract_database(sealed_small, manifest, "falsches-passwort", target)
    assert not target.exists()


# --- a full disk is not a damaged backup (v0.3.10 review of p125) ----------------------------------


class _FullDisk:
    """A file that takes a little and then reports a full disk."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        raise OSError(28, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("password", [None, "sicher-genug-1"])
def test_a_full_disk_while_extracting_is_an_oserror_not_a_damaged_backup(live, work, monkeypatch, password):
    """A write error is the machine's, not the file's: the page must not tell the person the backup is broken."""
    path = fb.create_backup(live, work_dir=work, password=password, now=NOW).path
    manifest = fb.read_manifest(path)
    real = fb.private_file

    @contextlib.contextmanager
    def full_disk(target):
        with real(target) as handle:
            yield _FullDisk(handle)

    monkeypatch.setattr(fb, "private_file", full_disk)
    target = work / "candidate.db"
    with pytest.raises(OSError) as raised:
        fb.extract_database(path, manifest, password, target)
    assert not isinstance(raised.value, fb.BackupError)
    assert not target.exists()


def test_a_damaged_zip_member_is_still_damaged(sealed_small, work):
    data = bytearray(sealed_small.read_bytes())
    # the stored member is the second one; break its CRC by changing a data byte AND keeping the sizes
    info = zipfile.ZipFile(sealed_small).getinfo(fb.SEALED)
    data[info.header_offset + 30 + len(fb.SEALED) + 10] ^= 1
    broken = work / "broken.zip"
    broken.write_bytes(bytes(data))
    with pytest.raises(fb.Damaged):
        fb.extract_database(broken, fb.read_manifest(broken), "sicher-genug-1", work / "candidate.db")
    assert not (work / "candidate.db").exists()
