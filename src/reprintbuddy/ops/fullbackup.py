"""The full backup (Spec 11.6): the whole database as one file, optionally encrypted.

Unlike the export (`ops/export.py`) it holds every secret: the Bambuddy key, the password
hash, the private HTTPS key and the cookie secret (Spec 11.5a names it as the exception).
Only the table of signed-in browsers is emptied, here and again before a restore: after a
restore everybody signs in with the account of the backup.

The file is a ZIP with exactly two members:
- `manifest.json`, always readable: format, versions, time, and how the data is protected.
  A restore can say what it is about to do before anybody types a password.
- `database.sqlite` (DEFLATE, with size and SHA-256 in the manifest), or
  `database.sqlite.enc`: the database compressed with zlib, then sealed with AES-256-GCM
  (`backupcrypto`), the exact manifest bytes as associated data.
  Compressing before encrypting leaks nothing here: nobody can feed chosen text into a
  backup and watch its size.

Reading is done in steps, each one refusing before the next costs anything:
`read_manifest` (nothing but the small manifest is read), `extract_database` (password,
integrity of the bytes), `verify_database` (SQLite integrity, revision, a trial migration on
a throw-away copy and a comparison of the structure with a new database). Only then does
`replace_database` back up the running database and swap the content.

An older database is restored as it is; the start of the service migrates it (Spec 11.6).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from reprintbuddy import __version__
from reprintbuddy.db import known_revisions, make_engine, upgrade
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops.backup import (
    KEEP_BACKUPS,
    backup_database,
    backup_dir_for,
    prune_backups,
    restore_database,
)

FORMAT = "reprintbuddy-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
PLAIN = "database.sqlite"
SEALED = "database.sqlite.enc"
CIPHER = "AES-256-GCM"
KDF_NAME = "argon2id"

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
KDF = bc.DEFAULT_KDF  # tests use smaller parameters

WORK_DIRNAME = "restore"
_CHUNK = 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[.+-][0-9A-Za-z.+-]*)?$")


class BackupError(Exception):
    """A backup that cannot be restored. `code` names the message the page shows."""

    code = "foreign"


class NotABackup(BackupError):
    code = "foreign"


class TooNew(BackupError):
    code = "too_new"


class TooLarge(BackupError):
    code = "too_large"


class PasswordNeeded(BackupError):
    code = "password_needed"


class WrongPassword(BackupError):
    code = "wrong_password"


class Damaged(BackupError):
    code = "damaged"


class Incompatible(BackupError):
    code = "incompatible"


@dataclass(frozen=True)
class Sealing:
    params: bc.KdfParams
    salt: bytes
    nonce: bytes
    check: bytes


@dataclass(frozen=True)
class Manifest:
    app_version: str
    schema_revision: str
    created_at: datetime
    encrypted: bool
    raw: bytes  # exactly as in the file: the associated data of the encryption
    size: int | None = None  # plain only
    sha256: str | None = None  # plain only
    sealing: Sealing | None = None  # encrypted only


@dataclass(frozen=True)
class CreatedBackup:
    path: Path  # in the work folder; the caller sends it and deletes it
    filename: str  # what the browser saves it as


# --- files ---------------------------------------------------------------------------------


def work_dir_for(db_path: Path) -> Path:
    return db_path.parent / WORK_DIRNAME


def ensure_work_dir(work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        work_dir.chmod(0o700)
    return work_dir


def clear_work_dir(work_dir: Path, *, keep: Iterable[Path] = ()) -> None:
    """Delete what earlier attempts left behind (a crash, a closed browser tab)."""
    kept = {Path(path).name for path in keep}
    if work_dir.is_dir():
        for path in work_dir.iterdir():
            if path.name not in kept and path.is_file():
                with contextlib.suppress(OSError):
                    path.unlink()


def remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        with contextlib.suppress(OSError):
            Path(f"{path}{suffix}").unlink()


@contextlib.contextmanager
def private_file(path: Path):
    """A new file only the service can read (0600 from the start, not chmod'ed later)."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        yield handle


def _chmod_private(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


# --- writing -------------------------------------------------------------------------------


def _snapshot(source: Path, target: Path) -> None:
    """A consistent, compact copy without the signed-in browsers.

    `VACUUM INTO` reads one consistent state (also what is still in the WAL) while the
    service goes on, and writes only live rows: free pages, which may still hold an
    overwritten key, stay behind. The emptied sessions are vacuumed away the same way.
    """
    remove_database_files(target)
    con = sqlite3.connect(str(source), timeout=30)
    try:
        con.execute("VACUUM INTO ?", (str(target),))
    finally:
        con.close()
    _chmod_private(target)
    con = sqlite3.connect(str(target))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        if _has_table(con, "sessions"):
            con.execute("DELETE FROM sessions")
            con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    found = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return found.fetchone() is not None


def _revision(path: Path) -> str:
    con = sqlite3.connect(str(path))
    try:
        return str(con.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    finally:
        con.close()


def _dump(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def create_backup(
    db_path: Path, *, work_dir: Path, password: str | None, now: datetime
) -> CreatedBackup:
    """Write a full backup into `work_dir` and return where it is and how to name it."""
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))  # sqlite3.connect would create an empty one
    ensure_work_dir(work_dir)
    token = secrets.token_hex(8)
    snapshot = work_dir / f"snapshot-{token}.db"
    try:
        _snapshot(db_path, snapshot)
        revision = _revision(snapshot)
        data = snapshot.read_bytes()
    finally:
        remove_database_files(snapshot)
    if len(data) > MAX_DATABASE_BYTES:
        raise TooLarge("the database is larger than a restore accepts")

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "schema_revision": revision,
        "created_at": now.isoformat(),
        "encrypted": bool(password),
    }
    if password:
        salt, nonce = bc.new_salt(), bc.new_nonce()
        key, check = bc.derive_key(password, salt, KDF)
        manifest["encryption"] = {
            "cipher": CIPHER,
            "kdf": KDF_NAME,
            "time_cost": KDF.time_cost,
            "memory_cost": KDF.memory_cost,
            "parallelism": KDF.parallelism,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "check": _b64(check),
        }
        raw = _dump(manifest)
        member, payload, method = SEALED, bc.seal(key, nonce, zlib.compress(data, 6), raw), zipfile.ZIP_STORED
    else:
        manifest["database"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        raw = _dump(manifest)
        member, payload, method = PLAIN, data, zipfile.ZIP_DEFLATED

    stamp = now.timetuple()[:6]
    path = work_dir / f"backup-{token}.zip"
    try:
        with private_file(path) as handle, zipfile.ZipFile(handle, "w") as archive:
            archive.writestr(zipfile.ZipInfo(MANIFEST, stamp), raw, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(zipfile.ZipInfo(member, stamp), payload, compress_type=method)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    suffix = "-encrypted" if password else ""
    return CreatedBackup(path, f"reprintbuddy-backup-{now:%Y%m%d-%H%M%S}{suffix}.zip")


# --- reading -------------------------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(version)
    return None if match is None else tuple(int(part) for part in match.groups())  # type: ignore[return-value]


# bzip2 and LZMA decompress a whole block per read, with no limit on the output: a few
# hundred bytes can ask for gigabytes. Only these two methods are read; the sealed database
# does not compress anyway and is always stored.
_METHODS = {
    MANIFEST: (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
    PLAIN: (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
    SEALED: (zipfile.ZIP_STORED,),
}
_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError)


def _copy_member(archive: zipfile.ZipFile, name: str, sink: Callable[[bytes], object], limit: int) -> int:
    """Pass one member to `sink` in chunks; more than `limit` bytes raise TooLarge."""
    total = 0
    try:
        with archive.open(name) as member:
            while chunk := member.read(_CHUNK):
                total += len(chunk)
                if total > limit:
                    raise TooLarge("member larger than allowed")
                sink(chunk)
    except _READ_ERRORS as error:
        raise Damaged("member cannot be read") from error
    return total


def _read_limited(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read one member, at most `limit` bytes, whatever its header claims."""
    buffer = bytearray()
    _copy_member(archive, name, buffer.extend, limit)
    return bytes(buffer)


def _decode(value: Any, size: int) -> bytes:
    if not isinstance(value, str):
        raise NotABackup("expected base64 text")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise NotABackup("bad base64") from error
    if len(data) != size:
        raise NotABackup("wrong length")
    return data


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotABackup("expected a whole number")
    return value


def _sealing(value: Any) -> Sealing:
    if not isinstance(value, dict) or value.get("cipher") != CIPHER or value.get("kdf") != KDF_NAME:
        raise NotABackup("unknown encryption")
    params = bc.KdfParams(
        time_cost=_int(value.get("time_cost")),
        memory_cost=_int(value.get("memory_cost")),
        parallelism=_int(value.get("parallelism")),
    )
    if not bc.params_allowed(params):
        raise NotABackup("key derivation parameters outside the limits")
    return Sealing(
        params=params,
        salt=_decode(value.get("salt"), bc.SALT_BYTES),
        nonce=_decode(value.get("nonce"), bc.NONCE_BYTES),
        check=_decode(value.get("check"), bc.KEY_BYTES),
    )


def _parse_manifest(raw: bytes, member: str) -> Manifest:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise NotABackup("manifest is no JSON") from error
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise NotABackup("not a Reprintbuddy backup")
    format_version = _int(data.get("format_version"))
    if format_version > FORMAT_VERSION:
        raise TooNew("made by a newer version")
    if format_version != FORMAT_VERSION:
        raise NotABackup("unknown format version")

    app_version = data.get("app_version")
    made_by = _version_tuple(app_version) if isinstance(app_version, str) else None
    if made_by is None:
        raise NotABackup("no version")
    running = _version_tuple(__version__)
    if running is not None and made_by > running:
        raise TooNew("made by a newer version")
    revision = data.get("schema_revision")
    if not isinstance(revision, str):
        raise NotABackup("no schema revision")
    if revision not in known_revisions():
        raise TooNew("database of a newer version")
    created = data.get("created_at")
    try:
        created_at = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError:
        created_at = None
    if created_at is None or created_at.tzinfo is None:
        raise NotABackup("no time")
    encrypted = data.get("encrypted")
    if not isinstance(encrypted, bool) or (member == SEALED) != encrypted:
        raise NotABackup("encryption flag does not match the content")

    if encrypted:
        return Manifest(app_version, revision, created_at, True, raw, sealing=_sealing(data.get("encryption")))
    database = data.get("database")
    if not isinstance(database, dict):
        raise NotABackup("no database description")
    size = _int(database.get("size"))
    sha256 = database.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise NotABackup("no checksum")
    return Manifest(app_version, revision, created_at, False, raw, size=size, sha256=sha256)


def read_manifest(path: Path) -> Manifest:
    """Check that the file is a backup this version can restore; read nothing big."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError) as error:
        raise NotABackup("not a ZIP file") from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) != 2 or names.count(MANIFEST) != 1 or (PLAIN in names) == (SEALED in names):
            raise NotABackup("unexpected content")
        member = SEALED if SEALED in names else PLAIN
        manifest_info = archive.getinfo(MANIFEST)
        member_info = archive.getinfo(member)
        if (manifest_info.flag_bits | member_info.flag_bits) & 0x1:
            raise NotABackup("password-protected ZIP entries")
        if (
            manifest_info.compress_type not in _METHODS[MANIFEST]
            or member_info.compress_type not in _METHODS[member]
        ):
            raise NotABackup("unexpected compression method")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise NotABackup("manifest too large")
        if member_info.file_size > MAX_DATABASE_BYTES + bc.TAG_BYTES:
            raise TooLarge("database too large")
        try:
            raw = _read_limited(archive, MANIFEST, MAX_MANIFEST_BYTES)
        except (Damaged, TooLarge) as error:
            raise NotABackup("manifest cannot be read") from error
    return _parse_manifest(raw, member)


def _inflate_to(packed: bytes, handle: IO[bytes]) -> None:
    """Decompress into the file chunk by chunk: the database is never twice in memory."""
    inflater = zlib.decompressobj()
    total = 0
    data = packed
    try:
        while True:
            chunk = inflater.decompress(data, _CHUNK)
            data = inflater.unconsumed_tail
            total += len(chunk)
            if total > MAX_DATABASE_BYTES:
                raise TooLarge("database too large")
            handle.write(chunk)
            if inflater.eof or not (data or chunk):
                break
    except zlib.error as error:
        raise Damaged("cannot decompress") from error
    if not inflater.eof or inflater.unused_data:
        raise Damaged("compressed data incomplete")


def extract_database(path: Path, manifest: Manifest, password: str | None, target: Path) -> None:
    """Write the database of the backup to `target`, or raise and leave no file behind."""
    with zipfile.ZipFile(path) as archive:
        if manifest.encrypted:
            sealing = manifest.sealing
            assert sealing is not None
            if not password:
                raise PasswordNeeded("the backup is encrypted")
            key, check = bc.derive_key(password, sealing.salt, sealing.params)
            if not bc.check_matches(check, sealing.check):
                raise WrongPassword("wrong password")
            # Stored, not compressed (see `_METHODS`): never larger than the uploaded file.
            sealed = _read_limited(archive, SEALED, MAX_DATABASE_BYTES + bc.TAG_BYTES)
            try:
                packed = bc.open_sealed(key, sealing.nonce, sealed, manifest.raw)
            except bc.DecryptError as error:
                raise Damaged("the data does not open") from error
            del sealed
            try:
                with private_file(target) as handle:
                    _inflate_to(packed, handle)
            except BaseException:
                remove_database_files(target)
                raise
            return
        assert manifest.size is not None
        if manifest.size > MAX_DATABASE_BYTES:
            raise TooLarge("database too large")
        digest = hashlib.sha256()
        try:
            with private_file(target) as handle:

                def sink(chunk: bytes) -> None:
                    digest.update(chunk)
                    handle.write(chunk)

                try:
                    written = _copy_member(archive, PLAIN, sink, manifest.size)
                except TooLarge as error:
                    raise Damaged("longer than the manifest says") from error
            if written != manifest.size or digest.hexdigest() != manifest.sha256:
                raise Damaged("checksum does not match")
        except BaseException:
            remove_database_files(target)
            raise


def _structure(path: Path) -> dict[str, Any]:
    """What a database consists of: its objects, and per table the columns (name, type,
    NOT NULL, primary key, generated), indexes (unique, origin, columns) and foreign keys.

    Not the SQL text: the text of the same table differs between the SQLAlchemy versions
    that created it, and an install updates its packages. Internal objects are the ones
    whose name starts with `sqlite_` (substr: in LIKE the `_` would match any character).
    Names go in as parameters of the table-valued pragmas, never into the SQL text.
    """
    con = sqlite3.connect(str(path))
    try:
        objects = sorted(
            con.execute(
                "SELECT type, name, tbl_name FROM sqlite_master WHERE substr(name, 1, 7) <> 'sqlite_'"
            ).fetchall()
        )
        tables: dict[str, Any] = {}
        for kind, name, _ in objects:
            if kind != "table":
                continue
            columns = con.execute(
                'SELECT name, upper(type), "notnull", pk, hidden FROM pragma_table_xinfo(?) ORDER BY cid',
                (name,),
            ).fetchall()
            indexes = []
            for index, unique, origin, partial in con.execute(
                'SELECT name, "unique", origin, partial FROM pragma_index_list(?)', (name,)
            ).fetchall():
                parts = con.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index,)
                ).fetchall()
                shown = "" if index.startswith("sqlite_autoindex_") else index
                indexes.append((shown, unique, origin, partial, tuple(parts)))
            keys = con.execute(
                'SELECT "table", "from", "to", on_update, on_delete '
                "FROM pragma_foreign_key_list(?) ORDER BY id, seq",
                (name,),
            ).fetchall()
            tables[name] = {"columns": columns, "indexes": sorted(indexes), "foreign_keys": keys}
    finally:
        con.close()
    return {"objects": objects, "tables": tables}


def verify_database(candidate: Path, manifest: Manifest, *, work_dir: Path) -> None:
    """Is this an intact Reprintbuddy database that this version can migrate and run?"""
    with open(candidate, "rb") as handle:
        if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise Damaged("not an SQLite database")
    try:
        con = sqlite3.connect(str(candidate))
        try:
            con.execute("PRAGMA cell_size_check=ON")
            if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise Damaged("integrity check failed")
            # A UTF-16 database breaks every connection the service already has open.
            if con.execute("PRAGMA encoding").fetchone() != ("UTF-8",):
                raise Incompatible("not UTF-8")
            try:
                found = con.execute("SELECT version_num FROM alembic_version").fetchall()
            except sqlite3.OperationalError as error:
                raise NotABackup("no Reprintbuddy database") from error
            con.execute("PRAGMA journal_mode=DELETE")
        finally:
            con.close()
    except sqlite3.DatabaseError as error:
        raise Damaged("not a readable database") from error
    if found != [(manifest.schema_revision,)]:
        raise NotABackup("database revision differs from the manifest")

    # A trial migration on a throw-away copy, compared with a database made from scratch.
    token = secrets.token_hex(8)
    trial = work_dir / f"trial-{token}.db"
    fresh = work_dir / f"fresh-{token}.db"
    try:
        shutil.copyfile(candidate, trial)
        _chmod_private(trial)
        engine = make_engine(trial)
        try:
            upgrade(engine)
        except Exception as error:  # noqa: BLE001 - any failure means: this one cannot run here
            raise Incompatible("the migration fails") from error
        finally:
            engine.dispose()
        engine = make_engine(fresh)
        try:
            upgrade(engine)
        finally:
            engine.dispose()
        try:
            differs = _structure(trial) != _structure(fresh)
        except sqlite3.Error as error:
            raise Incompatible("the structure cannot be read") from error
        if differs:
            raise Incompatible("the structure differs from a Reprintbuddy database")
    finally:
        remove_database_files(trial)
        remove_database_files(fresh)


# --- replacing -----------------------------------------------------------------------------


def _page_size(path: Path) -> int:
    con = sqlite3.connect(str(path), timeout=30)
    try:
        return int(con.execute("PRAGMA page_size").fetchone()[0])
    finally:
        con.close()


def _prepare(candidate: Path, page_size: int) -> None:
    """Empty the sessions and match the page size of the live database.

    SQLite cannot copy pages of another size into a database in WAL mode.
    """
    con = sqlite3.connect(str(candidate))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        if _has_table(con, "sessions"):
            con.execute("DELETE FROM sessions")
            con.commit()
        if int(con.execute("PRAGMA page_size").fetchone()[0]) != page_size:
            con.execute(f"PRAGMA page_size = {int(page_size)}")
            con.execute("VACUUM")
    finally:
        con.close()


def replace_database(
    candidate: Path,
    db_path: Path,
    *,
    now: datetime,
    protect: Iterable[Path] = (),
    keep: int = KEEP_BACKUPS,
) -> Path:
    """Back up the running database, copy the candidate into it, return the backup.

    Safe while the service has the database open: the copy uses SQLite's backup API in
    one step, so the running database is either the old or the new one, never a mix.
    """
    if not candidate.is_file():
        raise FileNotFoundError(str(candidate))
    _prepare(candidate, _page_size(db_path))
    backup_dir = backup_dir_for(db_path)
    before = backup_database(db_path, backup_dir, label="before-restore", now=now)
    restore_database(candidate, db_path)
    remove_database_files(candidate)
    prune_backups(backup_dir, keep, protect=[before, *protect])
    return before


def save_upload(source: IO[bytes], target: Path, *, limit: int = MAX_UPLOAD_BYTES) -> int:
    """Copy an uploaded file into the work folder, at most `limit` bytes."""
    total = 0
    try:
        with private_file(target) as handle:
            while chunk := source.read(_CHUNK):
                total += len(chunk)
                if total > limit:
                    raise TooLarge("upload too large")
                handle.write(chunk)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return total
