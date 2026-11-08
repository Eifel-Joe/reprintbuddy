"""The full backup file (Spec 11.6): write it, read it back, refuse anything else."""

import json
import os
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.db import known_revisions, make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb

NOW = datetime(2026, 11, 7, 12, 0, 5, tzinfo=UTC)
API_KEY = "bb_FULLBACKUP_test_key"
TLS_KEY = "-----BEGIN PRIVATE KEY-----\nFULLBACKUP\n-----END PRIVATE KEY-----\n"
FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


def make_db(path: Path, revision: str = "head") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(path)
    upgrade(engine, revision=revision)
    engine.dispose()
    return path


@pytest.fixture
def live(tmp_path):
    """A database as the service has it: secrets, an account, a signed-in browser."""
    path = make_db(tmp_path / "data" / "reprintbuddy.db")
    maker = make_sessionmaker(make_engine(path))
    with maker() as session:
        st.set_setting(session, "bambuddy_api_key", API_KEY)
        st.set_setting(session, "tls_key", TLS_KEY)
        st.set_setting(session, "queue_cap", 7)
        st.ensure_session_secret(session)
        auth.create_admin(session, "admin", "admin-password-1")
        auth.create_session(session)
    return path


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def rows(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def setting(path: Path, key: str):
    found = rows(path, f"SELECT value FROM settings WHERE key = '{key}'")
    return json.loads(found[0][0]) if found else None


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def write_zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def rewrite(source: Path, target: Path, **replace: bytes) -> Path:
    """Copy a backup with some members replaced (by name, dots as underscores)."""
    content = members(source)
    entries = [(name, replace.get(name.replace(".", "_"), data)) for name, data in content.items()]
    return write_zip(target, entries)


def manifest_of(path: Path) -> dict:
    return json.loads(members(path)["manifest.json"])


def with_manifest(source: Path, target: Path, **changes) -> Path:
    manifest = manifest_of(source)
    manifest.update(changes)
    return rewrite(source, target, manifest_json=json.dumps(manifest).encode())


def plain_backup(live, work) -> fb.CreatedBackup:
    return fb.create_backup(live, work_dir=work, password=None, now=NOW)


def sealed_backup(live, work, password="sicher-genug-1") -> fb.CreatedBackup:
    return fb.create_backup(live, work_dir=work, password=password, now=NOW)


def read_back(path: Path, work: Path, password: str | None = None) -> Path:
    manifest = fb.read_manifest(path)
    target = work / "candidate.db"
    fb.extract_database(path, manifest, password, target)
    fb.verify_database(target, manifest, work_dir=work)
    return target


# --- writing -------------------------------------------------------------------------------


def test_plain_backup_holds_everything_but_sessions(live, work):
    created = plain_backup(live, work)
    assert created.filename == "reprintbuddy-backup-20261107-120005.zip"
    assert set(members(created.path)) == {"manifest.json", "database.sqlite"}
    manifest = manifest_of(created.path)
    assert manifest["format"] == "reprintbuddy-backup"
    assert manifest["format_version"] == 1
    assert manifest["encrypted"] is False
    assert manifest["schema_revision"] == known_revisions()[-1]
    assert manifest["created_at"] == "2026-11-07T12:00:05+00:00"
    db = read_back(created.path, work)
    assert setting(db, "bambuddy_api_key") == API_KEY
    assert setting(db, "tls_key") == TLS_KEY
    assert setting(db, "session_secret") == setting(live, "session_secret")
    assert rows(db, "SELECT username, password_hash FROM users") == rows(
        live, "SELECT username, password_hash FROM users"
    )
    assert rows(db, "SELECT count(*) FROM sessions") == [(0,)]
    assert rows(live, "SELECT count(*) FROM sessions") == [(1,)]  # the running one is untouched


def test_encrypted_backup_hides_the_database(live, work):
    created = sealed_backup(live, work)
    assert created.filename == "reprintbuddy-backup-20261107-120005-encrypted.zip"
    content = members(created.path)
    assert set(content) == {"manifest.json", "database.sqlite.enc"}
    blob = b"".join(content.values())
    for secret in (API_KEY.encode(), b"FULLBACKUP", b"SQLite format 3", b"admin-password"):
        assert secret not in blob
    manifest = manifest_of(created.path)
    assert manifest["encrypted"] is True
    assert manifest["encryption"]["cipher"] == "AES-256-GCM"
    assert manifest["encryption"]["kdf"] == "argon2id"
    db = read_back(created.path, work, "sicher-genug-1")
    assert setting(db, "bambuddy_api_key") == API_KEY


def test_two_encrypted_backups_use_fresh_salt_and_nonce(live, work):
    first = manifest_of(sealed_backup(live, work).path)["encryption"]
    second = manifest_of(sealed_backup(live, work).path)["encryption"]
    assert first["salt"] != second["salt"] and first["nonce"] != second["nonce"]


def test_snapshot_drops_deleted_data(live, work):
    con = sqlite3.connect(live)
    con.execute("INSERT INTO settings (key, value) VALUES ('old', ?)", (json.dumps("OLD-SECRET-" * 50),))
    con.commit()
    con.execute("DELETE FROM settings WHERE key = 'old'")
    con.commit()
    con.close()
    created = plain_backup(live, work)
    assert b"OLD-SECRET-" not in members(created.path)["database.sqlite"]


def test_snapshot_sees_data_still_in_the_wal(live, work):
    holder = sqlite3.connect(live)  # keeps the WAL from being checkpointed away
    holder.execute("SELECT 1").fetchall()
    con = sqlite3.connect(live)
    con.execute("UPDATE settings SET value = '9' WHERE key = 'queue_cap'")
    con.commit()
    con.close()
    created = plain_backup(live, work)
    holder.close()
    assert setting(read_back(created.path, work), "queue_cap") == 9


def test_writing_leaves_no_temporary_files(live, work):
    created = plain_backup(live, work)
    assert sorted(p.name for p in work.iterdir()) == [created.path.name]


# --- reading: what is refused before any password -------------------------------------------


def test_not_a_zip(work):
    path = work / "x.zip"
    path.write_bytes(b"this is no zip file")
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_zip_from_somewhere_else(work):
    path = write_zip(work / "x.zip", [("photo.jpg", b"\xff\xd8")])
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_foreign_format_name(live, work):
    source = plain_backup(live, work).path
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(with_manifest(source, work / "y.zip", format="bambuddy-backup"))


def test_newer_format_version_is_too_new(live, work):
    source = plain_backup(live, work).path
    with pytest.raises(fb.TooNew):
        fb.read_manifest(with_manifest(source, work / "y.zip", format_version=2))


def test_newer_app_version_is_too_new(live, work, monkeypatch):
    monkeypatch.setattr(fb, "__version__", "0.3.5")
    source = plain_backup(live, work).path
    with pytest.raises(fb.TooNew):
        fb.read_manifest(with_manifest(source, work / "y.zip", app_version="0.4.0"))
    assert fb.read_manifest(with_manifest(source, work / "z.zip", app_version="0.3.5")).app_version == "0.3.5"
    assert fb.read_manifest(with_manifest(source, work / "w.zip", app_version="0.2.9")).app_version == "0.2.9"


def test_unknown_schema_revision_is_too_new(live, work):
    source = plain_backup(live, work).path
    with pytest.raises(fb.TooNew):
        fb.read_manifest(with_manifest(source, work / "y.zip", schema_revision="0999"))


@pytest.mark.parametrize(
    "changes",
    [
        {"app_version": "latest"},
        {"app_version": 5},
        {"schema_revision": None},
        {"created_at": "yesterday"},
        {"encrypted": "no"},
        {"encrypted": True},  # says encrypted, carries the plain database
    ],
)
def test_broken_manifest_fields(live, work, changes):
    source = plain_backup(live, work).path
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(with_manifest(source, work / "y.zip", **changes))


def test_manifest_that_is_no_json(live, work):
    source = plain_backup(live, work).path
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(rewrite(source, work / "y.zip", manifest_json=b"{not json"))


@pytest.mark.parametrize(
    "extra",
    [
        [("notes.txt", b"hello")],
        [("../evil.db", b"x")],
        [("manifest.json", b"{}")],  # a second member of the same name
    ],
)
@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_unexpected_members(live, work, extra):
    content = members(plain_backup(live, work).path)
    path = write_zip(work / "y.zip", list(content.items()) + extra)
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_both_database_members(live, work):
    content = members(plain_backup(live, work).path)
    path = write_zip(work / "y.zip", list(content.items()) + [("database.sqlite.enc", b"x" * 40)])
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_database_member_too_large(live, work, monkeypatch):
    source = plain_backup(live, work).path
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 1024)
    with pytest.raises(fb.TooLarge):
        fb.read_manifest(source)


@pytest.mark.parametrize(
    "encryption",
    [
        {"memory_cost": 4 * 1024 * 1024},
        {"time_cost": 100},
        {"parallelism": 64},
        {"salt": "AAAA"},
        {"nonce": "not base64!"},
        {"cipher": "ROT13"},
        {"kdf": "md5"},
    ],
)
def test_encryption_parameters_outside_limits(live, work, encryption):
    source = sealed_backup(live, work).path
    manifest = manifest_of(source)
    manifest["encryption"].update(encryption)
    path = rewrite(source, work / "y.zip", manifest_json=json.dumps(manifest).encode())
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_manifest_describes_what_the_page_shows(live, work):
    manifest = fb.read_manifest(sealed_backup(live, work).path)
    assert manifest.encrypted is True
    assert manifest.created_at == NOW
    assert manifest.schema_revision == known_revisions()[-1]


# --- reading: password and integrity -------------------------------------------------------


def test_encrypted_without_password(live, work):
    path = sealed_backup(live, work).path
    manifest = fb.read_manifest(path)
    for missing in (None, ""):
        with pytest.raises(fb.PasswordNeeded):
            fb.extract_database(path, manifest, missing, work / "c.db")
    assert not (work / "c.db").exists()


def test_wrong_password(live, work):
    path = sealed_backup(live, work).path
    with pytest.raises(fb.WrongPassword):
        fb.extract_database(path, fb.read_manifest(path), "falsches-passwort", work / "c.db")
    assert not (work / "c.db").exists()


def test_flipped_bit_in_encrypted_data(live, work):
    source = sealed_backup(live, work).path
    sealed = bytearray(members(source)["database.sqlite.enc"])
    sealed[len(sealed) // 2] ^= 1
    path = rewrite(source, work / "y.zip", database_sqlite_enc=bytes(sealed))
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", work / "c.db")
    assert not (work / "c.db").exists()


def test_changed_manifest_of_encrypted_backup(live, work):
    source = sealed_backup(live, work).path
    path = with_manifest(source, work / "y.zip", created_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), "sicher-genug-1", work / "c.db")


def test_changed_plain_database(live, work):
    source = plain_backup(live, work).path
    db = bytearray(members(source)["database.sqlite"])
    db[200] ^= 1
    path = rewrite(source, work / "y.zip", database_sqlite=bytes(db))
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), None, work / "c.db")


def test_plain_backup_ignores_a_given_password(live, work):
    path = plain_backup(live, work).path
    fb.extract_database(path, fb.read_manifest(path), "egal-123", work / "c.db")
    assert (work / "c.db").is_file()


def test_bytes_that_are_no_sqlite(live, work):
    source = plain_backup(live, work).path
    junk = b"no database here" * 100
    path = rewrite(source, work / "y.zip", database_sqlite=junk)
    manifest = manifest_of(path)
    import hashlib

    manifest["database"] = {"size": len(junk), "sha256": hashlib.sha256(junk).hexdigest()}
    path = rewrite(path, work / "z.zip", manifest_json=json.dumps(manifest).encode())
    with pytest.raises(fb.Damaged):
        read_back(path, work)


def test_database_revision_must_match_manifest(live, work):
    source = plain_backup(live, work).path
    path = with_manifest(source, work / "y.zip", schema_revision=known_revisions()[-2])
    with pytest.raises(fb.NotABackup):
        read_back(path, work)


def test_foreign_trigger_is_refused(tmp_path, work):
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TRIGGER sneaky AFTER INSERT ON journal BEGIN DELETE FROM settings; END")
    con.commit()
    con.close()
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(fb.Incompatible):
        read_back(created.path, work)


def test_missing_table_is_refused(tmp_path, work):
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("DROP TABLE reorder_marks")
    con.commit()
    con.close()
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(fb.Incompatible):
        read_back(created.path, work)


def test_older_database_is_accepted_but_not_migrated(tmp_path, work):
    older = known_revisions()[-2]
    path = make_db(tmp_path / "old" / "reprintbuddy.db", revision=older)
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    assert manifest_of(created.path)["schema_revision"] == older
    db = read_back(created.path, work)
    assert rows(db, "SELECT version_num FROM alembic_version") == [(older,)]  # the start migrates
    assert sorted(p.name for p in work.iterdir()) == sorted([created.path.name, db.name])


def test_corrupt_page_is_damaged(live, work):
    created = plain_backup(live, work)
    db = work / "plain.db"
    db.write_bytes(members(created.path)["database.sqlite"])
    page_size = rows(db, "PRAGMA page_size")[0][0]
    data = bytearray(db.read_bytes())
    for i in range(page_size * 2 + 8, page_size * 3 - 8):  # wreck the third page
        data[i] = 0xA5
    import hashlib

    manifest = manifest_of(created.path)
    manifest["database"] = {"size": len(data), "sha256": hashlib.sha256(bytes(data)).hexdigest()}
    path = write_zip(
        work / "y.zip",
        [("manifest.json", json.dumps(manifest).encode()), ("database.sqlite", bytes(data))],
    )
    with pytest.raises(fb.Damaged):
        read_back(path, work)


# --- replacing the live database -----------------------------------------------------------


def test_replace_keeps_a_backup_and_swaps_the_content(live, work, tmp_path):
    other = make_db(tmp_path / "other" / "reprintbuddy.db")
    maker = make_sessionmaker(make_engine(other))
    with maker() as session:
        st.set_setting(session, "queue_cap", 3)
        auth.create_admin(session, "restored", "restored-password-1")
        auth.create_session(session)
    candidate = read_back(fb.create_backup(other, work_dir=work, password=None, now=NOW).path, work)
    con = sqlite3.connect(candidate)  # a file made some other way may still carry sessions
    con.execute("INSERT INTO sessions VALUES ('x', '2026-11-07 12:00:00', '2026-11-07 12:00:00')")
    con.commit()
    con.close()

    service = make_sessionmaker(make_engine(live))
    holder = service()  # the service keeps a connection open
    holder.execute(text("SELECT 1"))
    before = fb.replace_database(candidate, live, now=NOW)
    holder.close()

    assert before.name == "reprintbuddy-before-restore-20261107T120005Z.db"
    assert setting(before, "queue_cap") == 7
    assert setting(live, "queue_cap") == 3
    assert rows(live, "SELECT username FROM users") == [("restored",)]
    assert rows(live, "SELECT count(*) FROM sessions") == [(0,)]
    assert setting(live, "session_secret") is None  # the other database never started a service
    assert not candidate.exists()


def test_replace_accepts_another_page_size(live, work, tmp_path):
    other = tmp_path / "other" / "reprintbuddy.db"
    other.parent.mkdir()
    con = sqlite3.connect(other)
    con.execute("PRAGMA page_size = 8192")
    con.execute("VACUUM")
    con.close()
    make_db(other)
    assert rows(other, "PRAGMA page_size") == [(8192,)]
    candidate = read_back(fb.create_backup(other, work_dir=work, password=None, now=NOW).path, work)
    fb.replace_database(candidate, live, now=NOW)
    assert rows(live, "PRAGMA page_size") == [(4096,)]
    assert rows(live, "PRAGMA integrity_check") == [("ok",)]


def test_replace_protects_a_backup_named_by_a_failed_rollback(live, work, tmp_path):
    from reprintbuddy.ops.backup import backup_database, backup_dir_for

    backups = backup_dir_for(live)
    named = backup_database(live, backups, label="v0.1.0", now=datetime(2026, 1, 1, tzinfo=UTC))
    for day in range(2, 8):
        backup_database(live, backups, label="v0.2.0", now=datetime(2026, 1, day, tzinfo=UTC))
    candidate = read_back(plain_backup(live, work).path, work)
    fb.replace_database(candidate, live, now=NOW, protect=[named])
    assert named.exists()
    assert len(list(backups.iterdir())) == 6  # five newest + the protected one


def test_replace_refuses_a_missing_candidate(live, work):
    with pytest.raises(FileNotFoundError):
        fb.replace_database(work / "nothing.db", live, now=NOW)
    assert not (live.parent / "backups").exists()


# --- review findings: crafted files ---------------------------------------------------------


def patch_central(path: Path, name: str, offset: int, value: bytes) -> None:
    """Overwrite a field of the central directory entry of `name` (see APPNOTE 4.3.12)."""
    data = bytearray(path.read_bytes())
    start = 0
    while True:
        start = data.index(b"PK\x01\x02", start)
        length = int.from_bytes(data[start + 28 : start + 30], "little")
        if data[start + 46 : start + 46 + length] == name.encode():
            data[start + offset : start + offset + len(value)] = value
            path.write_bytes(bytes(data))
            return
        start += 4


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_only_stored_or_deflated_members(live, work, method):
    """bzip2 and LZMA decompress whole blocks without an output limit (a ZIP bomb)."""
    content = members(plain_backup(live, work).path)
    path = work / "y.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", content["manifest.json"], compress_type=method)
        archive.writestr("database.sqlite", content["database.sqlite"])
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_unknown_compression_method(live, work):
    path = rewrite(plain_backup(live, work).path, work / "y.zip")
    patch_central(path, "manifest.json", 10, (9).to_bytes(2, "little"))  # deflate64
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_encrypted_member_must_be_stored(live, work):
    content = members(sealed_backup(live, work).path)
    path = work / "y.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", content["manifest.json"])
        archive.writestr("database.sqlite.enc", content["database.sqlite.enc"], compress_type=zipfile.ZIP_DEFLATED)
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_zip_password_flag(live, work):
    path = rewrite(plain_backup(live, work).path, work / "y.zip")
    patch_central(path, "database.sqlite", 8, (1).to_bytes(2, "little"))
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_inflated_database_is_counted(live, work, monkeypatch):
    path = sealed_backup(live, work).path
    manifest = fb.read_manifest(path)
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 4096)  # smaller than the database inside
    with pytest.raises(fb.TooLarge):
        fb.extract_database(path, manifest, "sicher-genug-1", work / "c.db")
    assert not (work / "c.db").exists()


def test_plain_database_is_counted(live, work, monkeypatch):
    path = plain_backup(live, work).path
    manifest = fb.read_manifest(path)
    monkeypatch.setattr(fb, "MAX_DATABASE_BYTES", 4096)
    with pytest.raises(fb.TooLarge):
        fb.extract_database(path, manifest, None, work / "c.db")
    assert not (work / "c.db").exists()


def test_plain_database_longer_than_the_manifest_says(live, work):
    source = plain_backup(live, work).path
    manifest = manifest_of(source)
    manifest["database"]["size"] = 1000
    path = rewrite(source, work / "y.zip", manifest_json=json.dumps(manifest).encode())
    with pytest.raises(fb.Damaged):
        fb.extract_database(path, fb.read_manifest(path), None, work / "c.db")
    assert not (work / "c.db").exists()


def schema_change(tmp_path, work, *statements: str):
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=OFF")
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()
    return fb.create_backup(path, work_dir=work, password=None, now=NOW).path


def test_object_named_like_an_internal_one_is_seen(tmp_path, work):
    path = schema_change(
        tmp_path, work, "CREATE TRIGGER sqliteXsneaky AFTER INSERT ON users BEGIN DELETE FROM settings; END"
    )
    with pytest.raises(fb.Incompatible):
        read_back(path, work)


def test_changed_columns_are_seen(tmp_path, work):
    path = schema_change(
        tmp_path,
        work,
        "CREATE TABLE users_new (id INTEGER, username BLOB, password_hash, PRIMARY KEY (id))",
        "INSERT INTO users_new SELECT * FROM users",
        "DROP TABLE users",
        "ALTER TABLE users_new RENAME TO users",
    )
    with pytest.raises(fb.Incompatible):
        read_back(path, work)


def test_dropped_index_is_seen(tmp_path, work):
    con = sqlite3.connect(make_db(tmp_path / "probe" / "p.db"))
    name = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    con.close()
    path = schema_change(tmp_path, work, f'DROP INDEX "{name}"')
    with pytest.raises(fb.Incompatible):
        read_back(path, work)


def test_odd_table_name_is_incompatible_not_an_error(tmp_path, work):
    path = schema_change(tmp_path, work, 'CREATE TABLE "a""b" (x)')
    with pytest.raises(fb.Incompatible):
        read_back(path, work)


def test_utf16_database_is_refused(tmp_path, work):
    path = tmp_path / "other" / "reprintbuddy.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute("PRAGMA encoding = 'UTF-16le'")
    con.execute("CREATE TABLE t (x)")
    con.execute("DROP TABLE t")
    con.commit()
    con.close()
    make_db(path)
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(fb.Incompatible):
        read_back(created.path, work)


# --- review findings: files on the disk ----------------------------------------------------


def test_save_upload_stops_at_the_limit(work):
    import io

    target = work / "upload.zip"
    with pytest.raises(fb.TooLarge):
        fb.save_upload(io.BytesIO(b"x" * 5000), target, limit=4096)
    assert not target.exists()
    assert fb.save_upload(io.BytesIO(b"x" * 4096), target, limit=4096) == 4096


def test_clear_work_dir(work):
    (work / "a.zip").write_bytes(b"1")
    (work / "b.db").write_bytes(b"2")
    fb.clear_work_dir(work, keep=[work / "b.db"])
    assert [p.name for p in work.iterdir()] == ["b.db"]


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_files_are_private(live, work):
    created = sealed_backup(live, work)
    assert created.path.stat().st_mode & 0o777 == 0o600
    target = work / "c.db"
    fb.extract_database(created.path, fb.read_manifest(created.path), "sicher-genug-1", target)
    assert target.stat().st_mode & 0o777 == 0o600


# --- security review: nothing of the schema comes from the file -----------------------------


def schema_of(path: Path) -> list[tuple]:
    return rows(path, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")


def test_hidden_trigger_written_into_the_schema_is_refused(tmp_path, work):
    """`writable_schema` can plant a trigger with a reserved name SQLite never lets one create."""
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) VALUES ('trigger', 'sqlite_bd', "
        "'users', 0, 'CREATE TRIGGER sqlite_bd AFTER UPDATE ON users BEGIN UPDATE users SET password_hash = ''x''; END')"
    )
    con.commit()
    con.close()
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(fb.Incompatible):
        read_back(created.path, work)


def test_restored_schema_is_built_from_the_migrations(tmp_path, work):
    """A CHECK (or a default, a collation …) the structure check does not compare never arrives."""
    path = schema_change(
        tmp_path,
        work,
        "CREATE TABLE users_new (id INTEGER NOT NULL, username VARCHAR NOT NULL, password_hash VARCHAR NOT NULL "
        "CHECK (password_hash = 'KNOWN'), PRIMARY KEY (id), UNIQUE (username))",
        "DROP TABLE users",
        "ALTER TABLE users_new RENAME TO users",
    )
    db = read_back(path, work)
    fresh = make_db(tmp_path / "fresh" / "reprintbuddy.db")
    assert schema_of(db) == schema_of(fresh)
    assert "KNOWN" not in str(schema_of(db))


def test_rows_are_copied_into_the_rebuilt_database(live, work):
    db = read_back(plain_backup(live, work).path, work)
    assert setting(db, "bambuddy_api_key") == API_KEY
    assert rows(db, "SELECT username FROM users") == [("admin",)]
    assert rows(db, "PRAGMA foreign_key_check") == []
    assert rows(db, "PRAGMA journal_mode") == [("delete",)]


def test_older_database_is_rebuilt_at_its_own_revision(tmp_path, work):
    older = known_revisions()[-2]
    path = make_db(tmp_path / "old" / "reprintbuddy.db", revision=older)
    db = read_back(fb.create_backup(path, work_dir=work, password=None, now=NOW).path, work)
    fresh = make_db(tmp_path / "fresh" / "reprintbuddy.db", revision=older)
    assert schema_of(db) == schema_of(fresh)


def test_virtual_table_is_refused_before_the_integrity_check(tmp_path, work):
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no FTS5")
    finally:
        con.close()
    path = schema_change(tmp_path, work, "CREATE VIRTUAL TABLE notes USING fts5(x)")
    with pytest.raises(fb.Incompatible):
        read_back(path, work)


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("queue_cap", "not json", fb.Damaged),
        ("queue_cap", json.dumps(-5), fb.Incompatible),
        ("bambuddy_url", json.dumps("ftp://somewhere"), fb.Incompatible),
        ("session_secret", json.dumps(42), fb.Incompatible),
        ("tls_key", json.dumps("-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n"), fb.Incompatible),
    ],
)
def test_settings_that_would_stop_the_start_are_refused(tmp_path, work, key, value, error):
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    if key == "tls_key":
        cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        con.execute("INSERT INTO settings (key, value) VALUES ('tls_cert', ?)", (json.dumps(cert),))
    con.commit()
    con.close()
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(error):
        read_back(created.path, work)


def test_a_valid_https_pair_is_accepted(tmp_path, work):
    from conftest import make_tls_pair

    cert, key = make_tls_pair()
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    maker = make_sessionmaker(make_engine(path))
    with maker() as session:
        st.set_setting(session, "tls_cert", cert)
        st.set_setting(session, "tls_key", key)
    read_back(fb.create_backup(path, work_dir=work, password=None, now=NOW).path, work)


def test_memory_cost_is_limited_to_the_default():
    assert not bc.params_allowed(bc.KdfParams(time_cost=3, memory_cost=128 * 1024, parallelism=4))


def test_a_vanished_candidate_never_becomes_an_empty_database(live, work, monkeypatch):
    """Between the check and the copy the file may go: SQLite must not create an empty one."""
    missing = work / "gone.db"
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    with pytest.raises(sqlite3.OperationalError):
        fb.replace_database(missing, live, now=NOW)
    assert not missing.exists()
    assert setting(live, "queue_cap") == 7


def test_cleanup_after_the_copy_does_not_fail_the_restore(live, work, monkeypatch):
    candidate = read_back(plain_backup(live, work).path, work)

    def broken(*args, **kwargs):
        raise OSError("cannot list the backups")

    monkeypatch.setattr(fb, "prune_backups", broken)
    fb.replace_database(candidate, live, now=NOW)  # no exception: the database is replaced
    assert rows(live, "SELECT count(*) FROM sessions") == [(0,)]


def test_clear_work_dir_can_keep_recent_files(work):
    import os as _os
    import time

    old = work / "upload-old.zip"
    new = work / "upload-new.zip"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    past = time.time() - 7200
    _os.utime(old, (past, past))
    fb.clear_work_dir(work, older_than=timedelta(hours=1))
    assert [p.name for p in work.iterdir()] == ["upload-new.zip"]


# --- final review -----------------------------------------------------------------------------


def test_virtual_table_in_odd_spelling_is_refused_by_its_kind(tmp_path, work):
    """`CREATE<tab>VIRTUAL` written into the schema passes a text match; the table list knows."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no FTS5")
    finally:
        con.close()
    path = make_db(tmp_path / "other" / "reprintbuddy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE VIRTUAL TABLE notes USING fts5(x)")
    con.execute("PRAGMA writable_schema=ON")
    con.execute("UPDATE sqlite_master SET sql = replace(sql, 'CREATE VIRTUAL', 'CREATE' || char(9) || 'VIRTUAL') WHERE name = 'notes'")
    con.commit()
    con.close()
    created = fb.create_backup(path, work_dir=work, password=None, now=NOW)
    with pytest.raises(fb.Incompatible, match="virtual tables"):
        read_back(created.path, work)


def test_only_stored_or_deflated_database_member(live, work):
    content = members(plain_backup(live, work).path)
    path = work / "y.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", content["manifest.json"])
        archive.writestr("database.sqlite", content["database.sqlite"], compress_type=zipfile.ZIP_BZIP2)
    with pytest.raises(fb.NotABackup):
        fb.read_manifest(path)


def test_an_untrusted_database_is_opened_defensively(tmp_path):
    """v0.3.9 p101: `DEFENSIVE` is set unconditionally (Python 3.12 and later): nothing can write into the
    schema of a crafted file, even with `writable_schema`."""
    path = make_db(tmp_path / "d.db")
    con = fb._open_untrusted(path)
    try:
        con.execute("PRAGMA writable_schema=ON")
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("UPDATE sqlite_master SET sql = sql WHERE name = 'settings'")
    finally:
        con.close()
