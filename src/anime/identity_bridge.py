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

Autobrr asks first through its External filter (POST /api/v1/check), which
answers like SMTP greylisting: 200 accepted, 422 rejected, 503 "ask again
later" while the identity process gets the real data from AniDB. A release
named as one already identified (manual match, recognized name) is answered
at once from local data.

A row in identity_requests is keyed by the torrent's info hash. Once decided it
keeps only its outcome for a day (KEEP_DONE_MS), so Autobrr's retries and its
later webhook action get the same answer and nothing is identified twice.
"""
import asyncio
import json
import logging
import sys
import time
import traceback

from src.anime.diagnostics import redact

TICK_S = 30                 # How often the API looks for due releases.
REPORT_GRACE_MS = 600_000   # A reported release is not tried again unless the API never applied it.
STOP_WAIT_S = 10
KEEP_DONE_MS = 86_400_000   # How long a decided release keeps its answer for Autobrr.
# Releases refused because the release itself is not supported (not an identity failure).
_NOT_SUPPORTED = frozenset(('RELEASE_MOVIE_SKIPPED', 'RELEASE_SPECIAL_UNSUPPORTED', 'RELEASE_TYPE_UNSUPPORTED',
                            'ANIDB_ENTRY_MISSING', 'ANIME_LISTS_ENTRY_MISSING'))


def now_ms():
    return time.time_ns() // 1_000_000


def trace(exc):
    """The full stack trace, credentials redacted (the log formatter keeps only line numbers)."""
    return redact(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[:8000]


def _infohash(raw_body):
    try:
        value = json.loads(raw_body).get('torrent_infohash')
    except (ValueError, UnicodeError, AttributeError):
        return None
    if type(value) is str and len(value) == 40 and all(c in '0123456789abcdef' for c in value):
        return value
    return None


def queue_release(db, raw_body, *, process_anyway=False, now=None):
    """Queue a release once per info hash, in its own transaction; returns its request ID.

    A release already waiting keeps its place, and one already decided keeps its
    answer. "Process anyway" queues a rejected release again.
    """
    from src.anime.storage.sqlite import transaction
    if type(raw_body) is not bytes or not 1 <= len(raw_body) <= 65536:
        raise ValueError('release body required')
    now = now_ms() if now is None else now
    infohash = _infohash(raw_body)
    with transaction(db):
        row = None if infohash is None else db.execute(
            'SELECT request_id,outcome FROM identity_requests WHERE infohash=?', (infohash,)).fetchone()
        if row is None:
            return db.execute(
                'INSERT INTO identity_requests(infohash,raw_body,process_anyway,received_at_ms,next_try_at_ms) '
                'VALUES(?,?,?,?,?)', (infohash, raw_body, 1 if process_anyway else 0, now, now)).lastrowid
        if process_anyway and row[1] is None:
            db.execute('UPDATE identity_requests SET process_anyway=1 WHERE request_id=?', (row[0],))
        elif process_anyway and row[1] == 'rejected':
            db.execute('UPDATE identity_requests SET raw_body=?,process_anyway=1,received_at_ms=?,next_try_at_ms=?,'
                       'tries=0,last_reason_code=NULL,outcome=NULL,decided_at_ms=NULL WHERE request_id=?',
                       (raw_body, now, now, row[0]))
        return row[0]


def status(db, request_id):
    """(outcome, reason_code): outcome is None while the release is still being identified."""
    row = db.execute('SELECT outcome,last_reason_code FROM identity_requests WHERE request_id=?',
                     (request_id,)).fetchone()
    return (None, None) if row is None or row[0] is None else (row[0], row[1])


def due(db, now=None):
    """Whether a release is waiting to be identified now."""
    now = now_ms() if now is None else now
    return db.execute('SELECT 1 FROM identity_requests WHERE outcome IS NULL AND next_try_at_ms<=? LIMIT 1',
                      (now,)).fetchone() is not None


def prune(db, now=None):
    """Forget decided releases after KEEP_DONE_MS; their delivery or notification remains."""
    from src.anime.storage.sqlite import transaction
    now = now_ms() if now is None else now
    with transaction(db):
        return db.execute('DELETE FROM identity_requests WHERE outcome IS NOT NULL AND decided_at_ms<?',
                          (now - KEEP_DONE_MS,)).rowcount


def known_series(database, raw_body):
    """The series of a release from local data only (manual match, recognized name), else None.

    The same first steps the identity process takes before it asks AniDB; both
    come from earlier verified AniDB identifications, so no lookup is needed.
    """
    from src.anime.decided_series import release_name
    from src.anime.providers.identity import _release_seasons
    from src.anime.release_names import release_title
    from src.anime.storage.manual_identity import for_release
    from src.anime.storage.recognition import recognized_for_release
    from src.anime.storage.job_queue import JobQueue
    title = release_name(raw_body)
    if title is None:
        return None
    seasons = {int(value) for value in _release_seasons(title)}
    if len(seasons) > 1 or not release_title(title)[0]:
        return None
    season = seasons.pop() if seasons else None
    manual = for_release(database, title, season)
    if manual is not None:
        return manual
    if season is None:
        return None
    with JobQueue(database).connection() as db:
        known = recognized_for_release(db, title, season=season)
    if known is None or known['tvdb_series_id'] is None:
        return None
    return {'schema_version': 2, 'anime': {'anidb_id': known['anidb_id'], 'tvdb_series_id': known['tvdb_series_id'],
                                           'title': known['title']}, 'episodes': [], 'attributes': {}}


def take_release(database, raw_body, logger, *, process_anyway=False):
    """Queue a release and answer from local data when it can: (request_id, outcome, reason_code, new).

    outcome None means the identity process must get the real data from AniDB first;
    new is True the first time this torrent is seen.
    """
    from src.anime.storage.job_queue import JobQueue
    with JobQueue(database).connection() as db:
        infohash = _infohash(raw_body)
        new = infohash is None or db.execute('SELECT 1 FROM identity_requests WHERE infohash=?',
                                             (infohash,)).fetchone() is None
        request_id = queue_release(db, raw_body, process_anyway=process_anyway)
        outcome = status(db, request_id)
    if outcome[0] is None:
        series = known_series(database, raw_body)
        if series is not None:
            apply_result(database, {'request_id': request_id, 'result': 'identified', 'series': series}, logger)
            with JobQueue(database).connection() as db:
                outcome = status(db, request_id)
    return (request_id, *outcome, new)


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
    """Apply one identity-process result in one transaction, then log it in plain words.

    An identified release wakes its series job with the torrent and the identity
    (no on/off switch; owner, 2026-11-26). Returns what was done: 'identified',
    'rejected', 'decided', or 'gone' (already applied).
    """
    from src.anime.decided_series import REASONS, WORDS, notify_refused, release_name
    from src.anime.diagnostics import log_history
    from src.anime.schema.anime_series import AnimeSeries
    from src.anime.storage.job_queue import JobQueue
    from src.anime.storage.review_decisions import decision
    from src.anime.series_job.store import add_torrent, wake
    from src.anime.storage.sqlite import transaction
    request_id = result['request_id']
    now = now_ms()
    records = []
    with JobQueue(database).connection() as db, transaction(db):
        row = db.execute('SELECT raw_body,process_anyway,received_at_ms,outcome,infohash FROM identity_requests '
                         'WHERE request_id=?', (request_id,)).fetchone()
        if row is None or row[3] is not None:
            return 'gone'                      # Already applied (a repeated report).
        raw, anyway, received, infohash = bytes(row[0]), bool(row[1]), row[2], row[4]
        name = release_name(raw) or f'Release {request_id}'

        def decide(outcome, reason_code):
            db.execute('UPDATE identity_requests SET outcome=?,last_reason_code=?,decided_at_ms=? '
                       'WHERE request_id=?', (outcome, reason_code, now, request_id))

        def rejected(reason_code):
            notify_not_processed(db, raw, reason_code, now)
            decide('rejected', reason_code)
            return 'rejected', f'{name}: not identified, {reason_code}; in API Reject'

        def apply():
            if result['result'] != 'identified':
                return rejected(result['reason_code'])
            series = AnimeSeries.from_dict(result['series']).to_dict()
            aid, title = series['anime']['anidb_id'], series['anime']['title']
            found = None if anyway else decision(db, aid)
            if found is not None:
                notify_refused(db, aid, found, raw, now=now)
                decide('rejected', REASONS[found])
                return 'decided', f'{name}: AniDB {aid} {title} is already {WORDS[found]}; in API Reject'

            if infohash is None:
                return rejected('API_WEBHOOK_INVALID')     # The webhook always stores the hash.
            wake(db, aid, title, now=now, reopen=anyway)
            add_torrent(db, aid, {'infohash': infohash, 'release_name': name, 'raw_body': raw,
                                  'identity': series}, now=now)
            decide('accepted', None)
            return 'identified', f'{name}: identified as AniDB {aid} {title}; woke series {aid}'
        done, line = apply()
    for record in records:
        log_history(logger, record)
    logger.info(line)
    return done


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
            prune(db)
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
        self.logger.info('Started the identity process for releases waiting on AniDB')
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    result = json.loads(line)
                    await self._io(self.apply, self.database, result, self.logger)
                except Exception as exc:  # Boundary: one bad result must not stop the others; full trace.
                    self.logger.error('Identity result could not be applied; the release is tried again '
                                      'later.\n%s', trace(exc))
            code = await self.process.wait()
            if code == 0:
                self.logger.info('The identity process finished: nothing left to identify')
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
