"""Service-side configuration repository; no HTTP server or worker.

Linux/Unix: one stable flock file serializes cooperative publishers. Readers
open the atomically replaced file for every operation. Use a worker thread
when invoking these blocking filesystem methods from async application code.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import logging
import os
from pathlib import Path
import stat
import uuid

from src.anime.configuration_schema import (
    CONFIGURATION_VERSION, FIELDS, MAX_BYTES, ConfigurationSnapshot,
    ConfigurationConflict, ConfigurationError, ConfigurationIOError,
    convert_document, decode, encode, for_operation, load_current, patch_snapshot,
    revision, source_version,
)
from src.anime.diagnostics import event

CONFIG_NAME = "settings.json"
LOCK_NAME = ".settings.lock"


def cli_config_dir():
    """The installed ADM launcher selects its branch; a release run directly uses the
    settings folder the installer recorded for it; otherwise the native default."""
    chosen = os.environ.get("ANIME_DISCOVERY_CONFIG_DIR")
    if chosen:
        return chosen
    release = Path(__file__).resolve().parents[2] / ".anime-discovery-release.json"
    try:
        import json
        recorded = json.loads(release.read_text()).get("config_dir")
    except (OSError, ValueError, AttributeError):
        recorded = None
    return recorded if isinstance(recorded, str) and recorded.startswith("/") else "/etc/anime-discovery"


@dataclass(frozen=True)
class UpdateResult:
    snapshot: ConfigurationSnapshot
    changed_fields: tuple[str, ...]
    restart_fields: tuple[str, ...]
    restart_components: tuple[str, ...]
    saved: bool
    durability_confirmed: bool
    backup_name: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class MigrationPlan:
    from_version: int
    to_version: int
    source_fingerprint: str
    required: bool


def _result(snapshot, changed=(), *, saved=False, durable=True, backup=None):
    restart = tuple(path for path in changed if FIELDS[path].apply_mode == "restart")
    components = tuple(sorted({component for path in restart
                               for component in FIELDS[path].restart_components}))
    return UpdateResult(snapshot, tuple(changed), restart, components, saved, durable, backup,
                        None if durable else "CONFIGURATION_DURABILITY_UNCONFIRMED")


def _regular(fd):
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationIOError()
    return metadata


def _read(directory_fd):
    # O_NONBLOCK prevents a malicious/nonregular FIFO from hanging the service
    # before fstat can reject it. No path lookup is performed through symlinks.
    fd = os.open(CONFIG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        metadata = _regular(stream.fileno())
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ConfigurationError()
    return raw, metadata


@contextmanager
def _directory(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, ValueError):
        raise ConfigurationIOError() from None
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _writer(path):
    with _directory(path) as directory_fd:
        fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        try:
            _regular(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory_fd
        finally:
            # Closing also releases flock, including on failure.
            os.close(fd)


def _write_bytes(directory_fd, name, raw, metadata=None):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as stream:
        if metadata is not None:
            # No privileged repair: service-owned files are an installation
            # requirement. Refuse ownership changes we cannot preserve.
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(directory_fd, snapshot, metadata=None):
    name = ".settings-" + uuid.uuid4().hex + ".tmp"
    try:
        _write_bytes(directory_fd, name, encode(snapshot), metadata)
        os.replace(name, CONFIG_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        # Publication happened. Do not raise a misleading "nothing saved"
        # error if the directory durability confirmation fails afterwards.
        try:
            os.fsync(directory_fd)
        except OSError:
            return False
        return True
    finally:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _backup(directory_fd, raw, metadata, version):
    name = "settings.json.before-v" + str(version) + "-" + uuid.uuid4().hex + ".bak"
    _write_bytes(directory_fd, name, raw, metadata)
    os.fsync(directory_fd)
    return name


def _log_result(result, *, migrated=False):
    logger = logging.getLogger("anime.api")
    if result.reason_code:
        event(logger, logging.WARNING, "CONFIGURATION_SAVE_UNCONFIRMED",
              reason_code=result.reason_code, component="api",
              config_revision=result.snapshot.revision)
    else:
        event(logger, logging.INFO, "CONFIGURATION_MIGRATED" if migrated else "CONFIGURATION_SAVED",
              component="api", config_revision=result.snapshot.revision)


class ConfigurationStore:
    """Only the management service publishes; jobs/API operations read snapshots.

    The directory must already exist and be writable by the service account.
    No chmod/chown of installed files, implicit creation on read, legacy import,
    credentials, SQLite migration, listener or process management is performed.
    """
    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self):
        try:
            with _directory(self.directory) as fd:
                raw, _ = _read(fd)
                return load_current(raw)
        except OSError:
            raise ConfigurationIOError() from None

    def operation(self, startup, *, component):
        return for_operation(self.read(), startup, component=component)

    def initialize(self, settings=None):
        """Explicit first-run creation; refuse replacing any existing file."""
        snapshot = ConfigurationSnapshot(1, {} if settings is None else settings)
        try:
            with _writer(self.directory) as fd:
                try:
                    _read(fd)
                except FileNotFoundError:
                    durable = _publish(fd, snapshot)
                else:
                    raise ConfigurationConflict()
        except OSError:
            raise ConfigurationIOError() from None
        result = _result(snapshot, saved=True, durable=durable)
        _log_result(result)
        return result

    def preview(self, changes, *, expected_revision):
        revision(expected_revision)
        current = self.read()
        if current.revision != expected_revision:
            raise ConfigurationConflict()
        candidate, changed = patch_snapshot(current, changes)
        return _result(candidate, changed)

    def update(self, changes, *, expected_revision):
        """Re-read under the stable lock; compare revision before publication."""
        revision(expected_revision)
        try:
            with _writer(self.directory) as fd:
                raw, metadata = _read(fd)
                current = load_current(raw)
                if current.revision != expected_revision:
                    raise ConfigurationConflict()
                candidate, changed = patch_snapshot(current, changes)
                if not changed:
                    return _result(current)
                durable = _publish(fd, candidate, metadata)
        except OSError:
            raise ConfigurationIOError() from None
        result = _result(candidate, changed, saved=True, durable=durable)
        _log_result(result)
        return result

    def preview_migration(self):
        """Read/validate/plan without writing settings, backups or lock files."""
        try:
            with _directory(self.directory) as fd:
                raw, _ = _read(fd)
        except OSError:
            raise ConfigurationIOError() from None
        document = decode(raw)
        version = source_version(document)
        convert_document(document)
        return MigrationPlan(version, CONFIGURATION_VERSION, hashlib.sha256(raw).hexdigest(),
                             version != CONFIGURATION_VERSION)

    def migrate(self, *, expected_fingerprint):
        """Explicit apply using a preview fingerprint; preserve original bytes."""
        if (type(expected_fingerprint) is not str or len(expected_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in expected_fingerprint)):
            raise ConfigurationError()
        try:
            with _writer(self.directory) as fd:
                raw, metadata = _read(fd)
                if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_fingerprint):
                    raise ConfigurationConflict()
                document = decode(raw)
                version = source_version(document)
                snapshot = convert_document(document)
                if version == CONFIGURATION_VERSION:
                    return _result(snapshot)
                backup = _backup(fd, raw, metadata, version)
                durable = _publish(fd, snapshot, metadata)
        except OSError:
            raise ConfigurationIOError() from None
        result = _result(snapshot, saved=True, durable=durable, backup=backup)
        _log_result(result, migrated=True)
        return result
