"""The full backup is written step by step, and its format did not change (v0.3.9 p102).

`create_backup` used to hold the database, its packed form and its sealed form in memory at the same
time (about three times the database). It now reads, packs and seals in blocks and writes straight into
the file. The file format stays: a backup made by an older version opens, and a new one equals what the
old way of writing made, byte for byte (same salt and nonce given).
"""

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
