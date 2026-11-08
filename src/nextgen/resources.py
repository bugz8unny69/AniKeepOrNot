"""One record of every file, folder and torrent a job creates (job design JD-1c).

Every job records what it creates and what happens to it through this module:
a torrent added to the client, the files it downloads, the files staged from
them, the library copy handed over at Keep, and every deletion. The records
show the work done on each file (`report`) and, from JD-5 on, are the only
thing cleanup works from.

JD-1c records only. Today's code still performs the file operations itself
and calls this module, inside its own transaction, at the point it commits
them. Nothing here touches the disk or the torrent client.
"""
from dataclasses import dataclass
import re

KINDS = ('TORRENT', 'DOWNLOADED_FILE', 'STAGED_FILE', 'FOLDER', 'LIBRARY_FILE')
_HASH = re.compile('[0-9a-f]{40}')


class ResourceInvalid(ValueError):
    """A record that cannot describe a real file or torrent; never written."""


@dataclass(frozen=True)
class Resource:
    resource_id: int
    job_id: int
    kind: str
    info_hash: str | None
    path: str | None
    size: int | None
    parent_id: int | None
    state: str


@dataclass(frozen=True)
class Operation:
    resource_id: int
    job_id: int
    operation: str
    occurred_at_ms: int


def _path(value):
    if (type(value) is not str or not value.startswith('/') or len(value) > 4096 or '\x00' in value
            or any(part in ('.', '..') for part in value.split('/')) or '//' in value or value == '/'):
        raise ResourceInvalid()
    return value.rstrip('/') if len(value) > 1 else value


def _size(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ResourceInvalid()
    return value


class Resources:
    """Records for one job, written inside the caller's transaction."""

    def __init__(self, db, job_id, now_ms):
        if not db.in_transaction:
            raise ValueError('resources are recorded inside the caller\'s transaction')
        if type(job_id) is not int or job_id <= 0 or type(now_ms) is not int or now_ms < 0:
            raise ResourceInvalid()
        self.db, self.job_id, self.now = db, job_id, now_ms

    def _operation(self, resource_id, operation):
        self.db.execute('INSERT INTO resource_operations(resource_id,job_id,operation,occurred_at_ms) '
                        'VALUES(?,?,?,?)', (resource_id, self.job_id, operation, self.now))

    def _record(self, kind, *, info_hash=None, path=None, size=None, parent_id=None, operation):
        if kind == 'TORRENT':
            found = self.db.execute("SELECT resource_id FROM resources WHERE job_id=? AND kind='TORRENT' "
                                    'AND info_hash=?', (self.job_id, info_hash)).fetchone()
        else:
            found = self.db.execute('SELECT resource_id FROM resources WHERE job_id=? AND kind=? AND path=?',
                                    (self.job_id, kind, path)).fetchone()
        if found is not None:                       # Recording again is a no-op: retries are safe.
            return found[0]
        resource_id = self.db.execute(
            'INSERT INTO resources(job_id,kind,info_hash,path,size,parent_id,state,created_at_ms,updated_at_ms) '
            "VALUES(?,?,?,?,?,?,'PRESENT',?,?)",
            (self.job_id, kind, info_hash, path, size, parent_id, self.now, self.now)).lastrowid
        self._operation(resource_id, operation)
        return resource_id

    def torrent(self, info_hash, *, save_path=None):
        """A torrent this job added to the client (and its save folder, if known)."""
        if type(info_hash) is not str or not _HASH.fullmatch(info_hash):
            raise ResourceInvalid()
        torrent = self._record('TORRENT', info_hash=info_hash, operation='ADDED')
        if save_path is not None:
            self._record('FOLDER', path=_path(save_path), parent_id=torrent, operation='ADDED')
        return torrent

    def downloaded(self, torrent_id, path, size):
        """A file the client downloads for this job's torrent."""
        return self._record('DOWNLOADED_FILE', path=_path(path), size=_size(size), parent_id=torrent_id,
                            operation='DOWNLOADED')

    def staged(self, path, size, *, source_id=None):
        """A file this job published to staging (from a download, when known)."""
        return self._record('STAGED_FILE', path=_path(path), size=_size(size), parent_id=source_id,
                            operation='STAGED')

    def library(self, path, size, *, source_id=None):
        """A copy imported to the library at Keep. It belongs to the user now."""
        resource = self._record('LIBRARY_FILE', path=_path(path), size=_size(size), parent_id=source_id,
                                operation='IMPORTED')
        self._set(resource, 'HANDED_OVER', 'HANDED_OVER')
        return resource

    def _set(self, resource_id, state, operation):
        current = self.db.execute('SELECT state FROM resources WHERE resource_id=?', (resource_id,)).fetchone()
        if current is None:
            raise ResourceInvalid()
        if current[0] == state:
            return False
        self.db.execute('UPDATE resources SET state=?,updated_at_ms=? WHERE resource_id=?',
                        (state, self.now, resource_id))
        self._operation(resource_id, operation)
        return True

    def deleted(self, resource_id):
        """The file, folder or torrent is gone (removed by this job, or already gone)."""
        return self._set(resource_id, 'DELETED', 'DELETED')

    def deleted_torrent(self, info_hash, *, with_data=True):
        """The torrent was removed from the client; with its data, its downloads are gone too."""
        changed = 0
        for resource_id, kind in self.db.execute(
                "SELECT resource_id,kind FROM resources WHERE info_hash=? AND kind='TORRENT' AND state='PRESENT'",
                (info_hash,)).fetchall():
            changed += self.deleted(resource_id)
            if with_data:
                for (child,) in self.db.execute(
                        "SELECT resource_id FROM resources WHERE parent_id=? AND kind IN ('DOWNLOADED_FILE','FOLDER') "
                        "AND state='PRESENT'", (resource_id,)).fetchall():
                    changed += self.deleted(child)
        return changed


def of_job(db, job_id):
    """Every resource a job created, oldest first."""
    return [Resource(*row) for row in db.execute(
        'SELECT resource_id,job_id,kind,info_hash,path,size,parent_id,state FROM resources '
        'WHERE job_id=? ORDER BY resource_id', (job_id,)).fetchall()]


def operations(db, resource_id):
    return [Operation(*row) for row in db.execute(
        'SELECT resource_id,job_id,operation,occurred_at_ms FROM resource_operations '
        'WHERE resource_id=? ORDER BY operation_id', (resource_id,)).fetchall()]


def report(db, job_ids):
    """Lines describing what was done to each file and torrent of these jobs."""
    lines = []
    for job_id in job_ids:
        for resource in of_job(db, job_id):
            what = resource.info_hash if resource.kind == 'TORRENT' else resource.path
            done = ' → '.join(o.operation.lower().replace('_', ' ') for o in operations(db, resource.resource_id))
            size = f' ({resource.size} bytes)' if resource.size is not None else ''
            lines.append(f'Job #{job_id:<6} {resource.kind.lower().replace("_", " ")}: {what}{size} · {done}')
    return lines
