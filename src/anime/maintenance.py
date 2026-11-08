"""Cooperative local-process admission and durable maintenance; Linux/Unix.

No jobs, clocks, HTTP or process launching. All participants must use the same
canonical database path on a local filesystem and keep admission for ALL work.
"""
from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path
import re
import stat
import uuid

from src.anime.diagnostics import event


class MaintenanceError(RuntimeError):
    reason_code = "MAINTENANCE_FAILED"
    def __init__(self):
        super().__init__("Maintenance state needs review.")


class MaintenancePending(MaintenanceError):
    reason_code = "MAINTENANCE_PENDING"
    def __init__(self):
        RuntimeError.__init__(self, "New work is paused for maintenance.")


class MaintenanceBusy(MaintenanceError):
    reason_code = "MAINTENANCE_BUSY"
    def __init__(self):
        RuntimeError.__init__(self, "Existing work is still finishing.")


def _ticket(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise MaintenanceError()
    return value


_LEASE_KEY = object()


class MaintenanceLease:
    """Capability valid only during its creating exclusive context.

    complete() requests admission reopening on clean context exit; callers must
    first validate compatibility/readiness. Exceptions always retain the marker.
    """
    def __init__(self, gate, *, _proof):
        if _proof is not _LEASE_KEY:
            raise MaintenanceError()
        self._gate = gate
        self._pid = os.getpid()
        self._active = True
        self._completed = False

    def require(self, database):
        if (not self._active or self._pid != os.getpid()
                or Path(database).absolute() != self._gate.database):
            raise MaintenanceError()

    def complete(self):
        self.require(self._gate.database)
        self._completed = True


class MaintenanceGate:
    def __init__(self, database):
        # Reject final symlinks; parent resolution establishes one shared scope.
        path = Path(database).absolute()
        self.database = path.parent.resolve() / path.name
        self._prefix = "." + path.name + ".nextgen-"

    @contextmanager
    def _directory(self):
        try:
            fd = os.open(self.database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise MaintenanceError() from None
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _lock(self, directory, suffix, mode):
        try:
            fd = os.open(self._prefix + suffix, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
        except OSError:
            raise MaintenanceError() from None
        try:
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise MaintenanceError()
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MaintenanceBusy() from None
            except OSError:
                raise MaintenanceError() from None
            yield
        finally:
            os.close(fd)

    def _read_marker(self, directory):
        try:
            return self._marker(directory)
        except OSError:
            raise MaintenanceError() from None

    def _marker(self, directory):
        try:
            fd = os.open(self._prefix + "pending", os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MaintenanceError()
            raw = stream.read(34)
        try:
            return _ticket(raw.decode("ascii"))
        except UnicodeError:
            raise MaintenanceError() from None

    def status(self):
        """Ticket for the trusted management service, not a public auth token."""
        with self._directory() as directory:
            with self._lock(directory, "admission", fcntl.LOCK_EX):
                return self._read_marker(directory)

    def request(self):
        """Durably stop new admissions; retry returns the same pending ticket."""
        try:
            return self._request()
        except OSError:
            raise MaintenanceError() from None

    def _request(self):
        with self._directory() as directory:
            with self._lock(directory, "admission", fcntl.LOCK_EX):
                current = self._read_marker(directory)
                if current is not None:
                    return current
                value = uuid.uuid4().hex
                fd = os.open(self._prefix + "pending", os.O_WRONLY | os.O_CREAT
                             | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value.encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
                event(logging.getLogger("anime.api"), logging.INFO,
                      "MAINTENANCE_REQUESTED", component="api")
                return value

    @contextmanager
    def operation(self):
        """Keep shared admission across the ENTIRE attempt/operation, not a query."""
        with self._directory() as directory:
            with self._lock(directory, "admission", fcntl.LOCK_EX):
                if self._read_marker(directory) is not None:
                    raise MaintenancePending()
                work = self._lock(directory, "work", fcntl.LOCK_SH)
                work.__enter__()
            try:
                yield
            finally:
                work.__exit__(None, None, None)

    @contextmanager
    def exclusive(self, ticket):
        """Nonblocking drain check; busy/failure retains pending state.

        The admission mutex stays held until exit so request/resume cannot race.
        Never fork, pass a lease to another process, or open nested admissions.
        """
        _ticket(ticket)
        with self._directory() as directory:
            with self._lock(directory, "admission", fcntl.LOCK_EX):
                if self._read_marker(directory) != ticket:
                    raise MaintenanceError()
                with self._lock(directory, "work", fcntl.LOCK_EX):
                    lease = MaintenanceLease(self, _proof=_LEASE_KEY)
                    try:
                        yield lease
                        if lease._completed:
                            self._resume(directory, ticket)
                    finally:
                        lease._active = False

    def _resume(self, directory, ticket):
        try:
            os.unlink(self._prefix + "pending", dir_fd=directory)
            try:
                os.fsync(directory)
            except OSError:
                # Reclose admission before releasing the mutex. A further
                # storage failure requires stopped-service recovery.
                fd = os.open(self._prefix + "pending", os.O_WRONLY | os.O_CREAT
                             | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(ticket.encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
                raise
        except OSError:
            raise MaintenanceError() from None
        event(logging.getLogger("anime.api"), logging.INFO,
              "MAINTENANCE_COMPLETED", component="api")
