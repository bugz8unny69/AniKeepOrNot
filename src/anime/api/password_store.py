"""Service-owned, atomic password record storage.

A dedicated file avoids modifying the legacy provider secrets schema. All
writers hold an advisory lock and use compare-and-swap revisions.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from src.anime.api.passwords import hash_password, verify_password

RECORD = "management-password.json"
LOCK = ".management-password.lock"


class PasswordConflict(Exception):
    pass


class PasswordStorageError(Exception):
    pass


class PasswordStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _open_dir(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(fd)
            raise PasswordStorageError("Unsafe password directory")
        return fd

    @staticmethod
    def _read(fd):
        try:
            file_fd = os.open(RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            return 0, None
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 2048):
                raise PasswordStorageError("Unsafe password record")
            raw = stream.read(2049)
        try:
            data = json.loads(raw)
            if (type(data) is not dict or set(data) != {"revision", "password_hash"}
                    or type(data["revision"]) is not int or not 1 <= data["revision"] < 2**63
                    or type(data["password_hash"]) is not str):
                raise ValueError()
            return data["revision"], data["password_hash"]
        except (ValueError, UnicodeError, TypeError):
            raise PasswordStorageError("Invalid password record") from None

    def read(self):
        try:
            fd = self._open_dir()
            try:
                return self._read(fd)
            finally:
                os.close(fd)
        except OSError:
            raise PasswordStorageError("Password record unavailable") from None

    def replace(self, password, *, expected_revision):
        # Hash before taking the lock, to minimize the serialized section.
        digest = hash_password(password)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid revision")
        try:
            fd = self._open_dir()
            try:
                lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
                with os.fdopen(lock_fd, "rb") as lock:
                    info = os.fstat(lock.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                            or info.st_mode & 0o077 or info.st_nlink != 1):
                        raise PasswordStorageError("Unsafe password lock")
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    revision, _ = self._read(fd)
                    if revision != expected_revision:
                        raise PasswordConflict()
                    if revision >= 2**63 - 1:
                        raise PasswordStorageError("Password revision exhausted")
                    next_revision = revision + 1
                    temp = ".management-password-" + uuid.uuid4().hex + ".tmp"
                    try:
                        temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
                        with os.fdopen(temp_fd, "wb") as stream:
                            stream.write(json.dumps({"revision": next_revision, "password_hash": digest}).encode() + bytes([10]))
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp, RECORD, src_dir_fd=fd, dst_dir_fd=fd)
                        os.fsync(fd)
                    finally:
                        try:
                            os.unlink(temp, dir_fd=fd)
                        except FileNotFoundError:
                            pass
                    return next_revision
            finally:
                os.close(fd)
        except OSError:
            raise PasswordStorageError("Password record unavailable") from None

    def verify(self, password):
        revision, digest = self.read()
        return revision, digest is not None and verify_password(password, digest)
