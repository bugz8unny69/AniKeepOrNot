"""Explicit NextGen-only SQLite upgrades under an active maintenance lease.

No implicit initialization, downgrade, legacy import, HTTP or worker execution.
Published scripts remain unchanged; each version commits independently.
"""
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from src.nextgen.diagnostics import event
from src.nextgen.maintenance import MaintenanceLease, MaintenanceError
from src.nextgen.storage.sqlite import SCHEMA

LATEST_VERSION = 52
SQL_DIR = Path(__file__).with_name("sql")
SCRIPTS = (
    "001_foundation.sql", "002_independent_jobs.sql", "003_incoming_events.sql",
    "004_diagnostic_components.sql", "005_job_statuses.sql", "006_api_diagnostics.sql",
    "007_worker_component.sql", "008_handler_history.sql", "009_provider_cache.sql", "010_release_verification.sql", "011_trial_windows.sql", "012_episode_reservations.sql", "013_acquisition_plans.sql", "014_acquisition_budgets.sql", "015_torrent_submissions.sql", "016_torrent_transfers.sql", "017_cancellation.sql", "018_management_actions.sql", "019_media_verifications.sql", "020_staging_publications.sql", "021_reject_decisions.sql", "022_keep_imports.sql", "023_reservation_release.sql", "024_recognized_seasons.sql", "025_recognized_episodes.sql", "026_recognized_work_aliases.sql", "027_verified_batches.sql", "028_batch_trial_plans.sql", "029_batch_episode_reservations.sql", "030_batch_torrent_submissions.sql", "031_batch_torrent_transfers.sql", "032_batch_probe_media.sql", "033_batch_torrent_unlocks.sql", "034_batch_probe_stops.sql", "035_batch_episode_media.sql", "036_batch_staging_publications.sql", "037_batch_keep_imports.sql", "038_batch_download_cleanup.sql", "039_batch_keep_cleanup.sql", "040_batch_submission_inventories.sql", "041_batch_cancellation_cleanup.sql", "042_sonarr_backfill.sql", "043_sonarr_selection.sql", "044_sonarr_candidates.sql", "045_sonarr_magnet_intents.sql", "046_observed_acquisition.sql", "047_released_magnet_intents.sql", "048_prowlarr_backfill_statuses.sql", "049_manual_identity_matches.sql", "050_series_rejection_reasons.sql", "051_batch_early_metadata.sql", "052_anime_title_metadata.sql",
)


class MigrationError(RuntimeError):
    reason_code = "DATABASE_MIGRATION_FAILED"
    def __init__(self):
        super().__init__("Database upgrade stopped. Maintenance remains active.")


class MigrationUnsupported(MigrationError):
    reason_code = "DATABASE_VERSION_UNSUPPORTED"
    def __init__(self):
        RuntimeError.__init__(self, "This database version cannot be upgraded by this application.")


class MigrationConflict(MigrationError):
    reason_code = "DATABASE_MIGRATION_CONFLICT"
    def __init__(self):
        RuntimeError.__init__(self, "The database upgrade plan changed. Preview it again.")


class MigrationSchemaInvalid(MigrationError):
    reason_code = "DATABASE_SCHEMA_INVALID"
    def __init__(self):
        RuntimeError.__init__(self, "The database does not match the expected application structure.")


@dataclass(frozen=True)
class MigrationPlan:
    from_version: int
    to_version: int
    schema_fingerprint: str
    scripts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied_versions: tuple[int, ...]
    backup_name: str | None


def _version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def _scripts():
    return tuple((name, (SQL_DIR / name).read_text(encoding="utf-8")) for name in SCRIPTS)


def _tokens(sql):
    # Preserve string literals; normalize insignificant whitespace, comments and
    # SQLite's identifier quoting after ALTER TABLE. Not a general SQL parser.
    parts = re.findall(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[^\s]", sql, re.S)
    result = []
    for part in parts:
        if part.startswith(("--", "/*")):
            continue
        if part.startswith("'"):
            result.append(part)
        else:
            result.append(part.strip('"').lower())
    # IF NOT EXISTS appears in fresh DDL but is omitted in table rebuilds.
    return tuple(item for i, item in enumerate(result)
                 if not any(result[start:start+3] == ["if", "not", "exists"]
                            for start in range(max(0, i-2), i+1)))


def _structure(db):
    return tuple((kind, name, owner, _tokens(sql)) for kind, name, owner, sql in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name") if sql is not None)


def _reference(version, scripts):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        if version == LATEST_VERSION:
            db.executescript(SCHEMA)
        else:
            for _, sql in scripts[:version]:
                db.executescript(sql)
        return _structure(db)
    finally:
        db.close()


def _validate(db, version, scripts):
    if _version(db) != version or _structure(db) != _reference(version, scripts):
        raise MigrationSchemaInvalid()
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationSchemaInvalid()
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise MigrationSchemaInvalid()


def _plan(db, scripts):
    version = _version(db)
    # v0 belongs to explicit initialization. Never guess an unversioned legacy DB.
    if not 1 <= version <= LATEST_VERSION:
        raise MigrationUnsupported()
    _validate(db, version, scripts)
    fingerprint = hashlib.sha256(json.dumps(_structure(db)).encode("utf-8")).hexdigest()
    steps = tuple((name, hashlib.sha256(sql.encode("utf-8")).hexdigest())
                  for name, sql in scripts[version:])
    return MigrationPlan(version, LATEST_VERSION, fingerprint, steps)


def _open(database, *, readonly, timeout_ms=0):
    path = Path(database).absolute()
    if not stat.S_ISREG(path.lstat().st_mode):
        raise MigrationSchemaInvalid()
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                         uri=True, timeout=timeout_ms/1000)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={timeout_ms}")
        if not readonly:
            db.execute("PRAGMA synchronous=FULL")
        return db
    except BaseException:
        db.close()
        raise


def _statements(sql):
    statement = ""
    for char in sql:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if _tokens(statement):
        raise MigrationError()


def _apply_step(db, sql, version, scripts):
    """Honor historical transaction boundaries; validate BEFORE their COMMIT."""
    committed = False
    try:
        for statement in _statements(sql):
            if _tokens(statement) == ("commit", ";"):
                if committed or not db.in_transaction:
                    raise MigrationError()
                _validate(db, version, scripts)
                committed = True
            db.execute(statement)
        if db.in_transaction or not committed or _version(db) != version:
            raise MigrationError()
    finally:
        if db.in_transaction:
            db.rollback()
        # In particular, v5 disables enforcement before BEGIN. Restore on every
        # path; the dedicated connection is also closed by the runner.
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise MigrationError()


def _backup(db, database, version, scripts):
    path = Path(database)
    name = path.name + ".before-v" + str(version) + "-" + uuid.uuid4().hex + ".sqlite"
    target = path.with_name(name)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(fd)
    destination = None
    try:
        destination = sqlite3.connect(target, timeout=0)
        def progress(status, remaining, total):
            if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
                # backup() otherwise retries BUSY/LOCKED indefinitely even
                # though the connection's busy timeout is zero.
                raise MigrationError()
        db.backup(destination, pages=128, progress=progress, sleep=0)
        _validate(destination, version, scripts)
    finally:
        if destination is not None:
            destination.close()
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return name


def preview(database):
    """Existing database only. No schema changes, backup or implicit creation.

    A caller using this outside exclusive maintenance must hold gate.operation().
    The short read transaction makes version/schema/integrity a single snapshot.
    """
    db = None
    try:
        scripts = _scripts()
        db = _open(database, readonly=True)
        db.execute("BEGIN")
        return _plan(db, scripts)
    except (OSError, sqlite3.Error, UnicodeError):
        raise MigrationError() from None
    finally:
        if db is not None:
            db.close()


def apply(database, plan, *, lease):
    """Upgrade under drained exclusive maintenance; NEVER reopen admission.

    A completed return is not sufficient to resume incompatible old processes.
    The management service must arrange compatible startup/readiness, then call
    lease.complete(). Any failure leaves the durable maintenance marker intact.
    """
    if not isinstance(lease, MaintenanceLease):
        raise MaintenanceError()
    lease.require(database)
    if type(plan) is not MigrationPlan:
        raise MigrationConflict()
    db = None
    try:
        scripts = _scripts()
        db = _open(database, readonly=False)
        current = _plan(db, scripts)
        if current != plan:
            raise MigrationConflict()
        if current.from_version == LATEST_VERSION:
            return MigrationResult(LATEST_VERSION, LATEST_VERSION, (), None)
        backup = _backup(db, database, current.from_version, scripts)
        applied = []
        for version in range(current.from_version + 1, LATEST_VERSION + 1):
            lease.require(database)
            if _version(db) != version - 1:
                raise MigrationConflict()
            _apply_step(db, scripts[version-1][1], version, scripts)
            applied.append(version)
        _validate(db, LATEST_VERSION, scripts)
        event(logging.getLogger("nextgen.api"), logging.INFO,
              "DATABASE_MIGRATED", component="api")
        return MigrationResult(current.from_version, LATEST_VERSION, tuple(applied), backup)
    except (OSError, sqlite3.Error, UnicodeError):
        raise MigrationError() from None
    finally:
        if db is not None:
            db.close()
