"""Identity bridge: the API identifies a release, then gives the worker a job.

Decided with the owner, 2026-11-22:

- The webhook stays fast. It refuses a release for a decided series from local
  data (API Reject, as before) and queues anything else in identity_requests.
- AniDB/TVDB lookups are paced and slow, so they run in a separate identity
  process (src/anime/identity_process.py) that the API starts when releases
  are waiting. It reports each result back to the API on its stdout, one JSON
  line per release, and exits when nothing is due.
- The API applies each result. Identified: the delivery, its identity and the
  worker's first job are saved in one transaction. Anything the worker could
  not complete (not identified, a movie, an unsupported release, a decided
  series) is refused with API Reject: a notification that says why and keeps
  the release for "Process anyway". The worker never sees it.

A row in identity_requests lives only until its release is identified or
refused; unidentified releases are not kept in intake.
"""
import asyncio
import json
import logging
import sys
import time
import traceback

from src.anime.diagnostics import redact

RELEASE_KIND = 'VERIFY_RELEASE'
TICK_S = 30                 # How often the API looks for due releases.
REPORT_GRACE_MS = 600_000   # A reported release is not tried again unless the API never applied it.
STOP_WAIT_S = 10
# Releases refused because the release itself is not supported (not an identity failure).
_NOT_SUPPORTED = frozenset(('RELEASE_MOVIE_SKIPPED', 'RELEASE_SPECIAL_UNSUPPORTED', 'RELEASE_TYPE_UNSUPPORTED',
                            'ANIDB_ENTRY_MISSING', 'ANIME_LISTS_ENTRY_MISSING'))


def now_ms():
    return time.time_ns() // 1_000_000


def trace(exc):
    """The full stack trace, credentials redacted (the log formatter keeps only line numbers)."""
    return redact(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[:8000]


def queue_release(db, raw_body, *, process_anyway=False, now=None):
    """Queue a release for the identity process, in its own transaction; returns its request ID."""
    from src.anime.storage.sqlite import transaction
    if type(raw_body) is not bytes or not 1 <= len(raw_body) <= 65536:
        raise ValueError('release body required')
    now = now_ms() if now is None else now
    with transaction(db):
        return db.execute(
            'INSERT INTO identity_requests(raw_body,process_anyway,received_at_ms,next_try_at_ms) VALUES(?,?,?,?)',
            (raw_body, 1 if process_anyway else 0, now, now)).lastrowid


def due(db, now=None):
    """Whether a release is waiting to be identified now."""
    now = now_ms() if now is None else now
    return db.execute('SELECT 1 FROM identity_requests WHERE next_try_at_ms<=? LIMIT 1', (now,)).fetchone() is not None


def notify_not_processed(db, raw_body, reason_code, now=None):
    """API Reject for a release the worker could not complete: why, and the release kept."""
    from src.anime.decided_series import release_name
    from src.anime.notifications import notify
    from src.anime.reasons import get_reason
    from src.anime.release_names import norm
    name = release_name(raw_body) or 'A release'
    reason = get_reason(reason_code)
    why = reason.message if reason else reason_code
    action = (' ' + reason.action) if reason and reason.action else ''
    lead = 'Not processed: ' if reason_code in _NOT_SUPPORTED else 'Not identified: '
    notify(db, {'type': 'API_REJECTION', 'severity': 'attention', 'title': (lead + name)[:120],
                'message': (f'{name[:300]} was not processed: {why}{action}')[:500],
                'release_name': name[:1024], 'group_key': ('release:' + (norm(name) or name))[:200],
                'raw_body': raw_body}, now=now)


def apply_result(database, result, logger):
    """Apply one identity-process result in one transaction. Returns what was done."""
    from src.anime.decided_series import notify_refused
    from src.anime.diagnostics import log_history
    from src.anime.schema.anime_series import AnimeSeries
    from src.anime.storage.job_queue import JobQueue
    from src.anime.storage.review_decisions import decision
    from src.anime.storage.sqlite import (IdentityConflict, IdentityNotReady, append_event_history,
                                          enqueue_job_with_receipt, persist_identity, transaction)
    request_id = result['request_id']
    now = now_ms()
    records = []
    with JobQueue(database).connection() as db, transaction(db):
        row = db.execute('SELECT raw_body,process_anyway,received_at_ms FROM identity_requests WHERE request_id=?',
                         (request_id,)).fetchone()
        if row is None:
            return 'gone'                      # Already applied (a repeated report).
        raw, anyway, received = bytes(row[0]), bool(row[1]), row[2]
        db.execute('DELETE FROM identity_requests WHERE request_id=?', (request_id,))
        if result['result'] != 'identified':
            notify_not_processed(db, raw, result['reason_code'], now)
            return 'rejected'
        series = AnimeSeries.from_dict(result['series']).to_dict()
        aid = series['anime']['anidb_id']
        found = None if anyway else decision(db, aid)
        if found is not None:
            notify_refused(db, aid, found, raw, now=now)
            return 'decided'
        db.execute('SAVEPOINT identity')   # A conflict undoes only the delivery, not the refusal.
        event_id = db.execute('INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)',
                              ('autobrr', raw, received)).lastrowid
        records.append(append_event_history(db, event_id=event_id, event_code='ACCEPTED', component='api',
                                            occurred_at_ms=now))
        try:
            _, audit = persist_identity(db, event_id, series, now)
        except (IdentityConflict, IdentityNotReady) as exc:
            db.execute('ROLLBACK TO identity'); db.execute('RELEASE identity')
            records.clear()
            notify_not_processed(db, raw, exc.reason_code, now)
            return 'rejected'
        if audit is not None:
            records.append(audit)
        _, created = enqueue_job_with_receipt(db, kind=RELEASE_KIND, payload={'anidb_id': aid},
                                              component='api', event_id=event_id, available_at_ms=now)
        records.append(created)
        db.execute('RELEASE identity')
    for record in records:
        log_history(logger, record)
    return 'identified'


class IdentityBridge:
    """Starts the identity process when releases are due and applies what it reports."""

    def __init__(self, config_directory, database, logger, *, apply=apply_result, command=None, executor=None):
        self.config_directory = str(config_directory)
        self.database = database
        self.logger = logger
        self.apply = apply
        self.command = command or (sys.executable, '-m', 'src.anime.identity_process',
                                   '--config-dir', self.config_directory)
        self.executor = executor
        self.process = None
        self._wake = None
        self._stopping = False

    def wake(self):
        if self._wake is not None:
            self._wake.set()

    async def _io(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    def _due(self):
        from src.anime.storage.job_queue import JobQueue
        with JobQueue(self.database).connection() as db:
            return due(db)

    async def run(self):
        """Until close(): start the identity process whenever releases are due and none is running."""
        self._wake = asyncio.Event()
        while not self._stopping:
            if self.process is None and await self._io(self._due):
                await self._serve()
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _serve(self):
        """Run one identity process to its end, applying each line it reports."""
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    result = json.loads(line)
                    done = await self._io(self.apply, self.database, result, self.logger)
                    self.logger.info('Identity result for release request %s: %s',
                                     result.get('request_id'), done)
                except Exception as exc:  # Boundary: one bad result must not stop the others; full trace.
                    self.logger.error('Identity result could not be applied; the release is tried again '
                                      'later.\n%s', trace(exc))
            code = await self.process.wait()
            if code != 0 and not self._stopping:
                self.logger.error('Identity process exited with status %s; its error is in the log above. '
                                  'Waiting releases are tried again in %ss.', code, TICK_S)
                await asyncio.sleep(TICK_S)
        finally:
            self.process = None

    async def close(self):
        self._stopping = True
        self.wake()
        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_WAIT_S)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
