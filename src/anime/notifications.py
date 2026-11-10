"""Notifications: "something needs your attention" (work item P3).

notify(db, event) is the interface the API and the worker call. It trusts its
callers: the event contract below is checked by regression tests, not at
runtime. It runs in the caller's transaction (a rolled-back action leaves no
notification) and never does network I/O.

Notifiers implement the interface. Generic is always on: it stores every event
in the inbox shown under the dashboard bell. Each enabled extra channel (ntfy,
a later item) is called after it; channels only push, never store.

Event fields (callers' contract):
  type      required  one of EVENT_TYPES
  title     required  <=120 chars, plain text
  message   required  <=500 chars, plain text; no paths, keys or raw errors
  anidb_id, job_id    optional positive ints
  release_name        optional, <=1024
  group_key           optional, <=200; merges with the inbox item that has it
  severity            optional 'info' (default) or 'attention'
  raw_body            optional, <=64 KiB, API_REJECTION only
"""
import time

from src.anime.storage import notifications as inbox

# Each type and the inbox actions it allows. Adding a type is one entry here
# (and in the notifications table CHECK).
EVENT_TYPES = {
    'API_REJECTION': ('process_anyway', 'add_release', 'open_series'),
    'SERIES_EPISODE_TROUBLE': ('open_series', 'open_job'),
    'KEEP_SEARCH_FAILED': ('open_job',),
    'TRANSCODE_BATCH_DONE': ('open_jobs',),
}


class Generic:
    """Stores every event in the inbox; severity is stored, not acted on."""
    def deliver(self, db, event, now):
        inbox.save(db, event, now)


GENERIC = Generic()
# Extra push channels, each enabled by its own Settings checkbox (ntfy later).
CHANNELS = ()


def notify(db, event, *, now=None):
    now = time.time_ns() // 1000000 if now is None else now
    GENERIC.deliver(db, event, now)
    for channel in CHANNELS:
        channel.deliver(db, event, now)


# --- Producers -------------------------------------------------------------------

# Jobs that work on getting one episode of a tracked series.
EPISODE_KINDS = frozenset({
    'VERIFY_RELEASE', 'SELECT_USER_EVALUATION', 'RESERVE_EPISODE', 'PREPARE_ACQUISITION',
    'SUBMIT_TORRENT', 'DOWNLOAD_TORRENT', 'VERIFY_MEDIA', 'TRANSCODE_MEDIA', 'STAGE_USER_EVALUATION_MEDIA',
    'DISCOVER_EPISODE_RELEASES', 'RESET_EPISODE_SEARCH',
    'EVALUATE_BATCH', 'RESERVE_BATCH', 'SUBMIT_BATCH', 'TRANSFER_BATCH', 'UNLOCK_BATCH',
    'VERIFY_BATCH_PROBE', 'VERIFY_BATCH_EPISODE', 'STAGE_BATCH_EPISODE'})


def _series_of_job(db, job_id):
    """The AniDB anime a job works for, from its delivery's identity or its plan."""
    row = db.execute(
        "SELECT r.anidb_id FROM jobs j JOIN identity_results r ON r.event_id=j.event_id "
        "WHERE j.job_id=? ORDER BY r.result_id DESC LIMIT 1", (job_id,)).fetchone()
    if row is None:
        row = db.execute(
            "SELECT p.anidb_id FROM jobs j JOIN user_evaluation_plans p "
            "ON p.plan_id=json_extract(j.payload_json,'$.plan_id') WHERE j.job_id=?", (job_id,)).fetchone()
    return None if row is None else row[0]


def _trouble_episodes(db, anidb_id):
    """AniDB episodes of the series that Anime Discovery could not get, as things stand."""
    return sorted({row[0] for row in db.execute(
        "SELECT r.anidb_episode_number FROM episode_discovery_results r JOIN user_evaluation_plans p USING(plan_id) "
        "WHERE p.anidb_id=? AND r.status='NO_RESULTS' "
        "UNION SELECT v.anidb_episode_number FROM release_verifications v JOIN jobs vj ON vj.job_id=v.job_id "
        "JOIN jobs j ON j.event_id=vj.event_id WHERE v.anidb_id=? AND j.state='NEEDS_REVIEW' "
        "UNION SELECT CAST(json_extract(j.payload_json,'$.exhausted_episode') AS INTEGER) FROM jobs j "
        "JOIN user_evaluation_plans p ON p.plan_id=json_extract(j.payload_json,'$.plan_id') "
        "WHERE p.anidb_id=? AND j.state='NEEDS_REVIEW' AND json_extract(j.payload_json,'$.exhausted_episode') IS NOT NULL",
        (anidb_id, anidb_id, anidb_id)) if row[0] is not None})


def series_trouble(db, anidb_id, *, job_id=None, now=None):
    """One inbox item per series listing the episodes it could not get."""
    title = db.execute('SELECT title FROM identified_anime WHERE anidb_id=?', (anidb_id,)).fetchone()
    if title is None:
        return
    name = title[0].strip()[:80]
    episodes = _trouble_episodes(db, anidb_id)
    if not episodes:
        text = f'{name}: trouble getting an episode.'
    else:
        listed = ', '.join(str(n) for n in episodes[:40]) + (' and more' if len(episodes) > 40 else '')
        text = f"{name}: trouble getting episode{'s' if len(episodes) > 1 else ''} {listed}."
    notify(db, {'type': 'SERIES_EPISODE_TROUBLE', 'title': f'Episode trouble: {name}'[:120],
                'message': text[:500], 'anidb_id': anidb_id, 'job_id': job_id,
                'group_key': f'series:{anidb_id}', 'severity': 'attention'}, now=now)


def job_needs_review(db, claim, now=None):
    """An episode job of a tracked series stopped in Needs review."""
    if claim.kind not in EPISODE_KINDS:
        return
    anidb_id = _series_of_job(db, claim.job_id)
    if anidb_id is not None:
        series_trouble(db, anidb_id, job_id=claim.job_id, now=now)


def keep_search_failed(db, claim, now=None):
    """Keep finished, but Sonarr's one missing-episode search did not start (P1 1.3)."""
    anidb_id = _series_of_job_keep(db, claim.job_id)
    name = None
    if anidb_id is not None:
        row = db.execute('SELECT title FROM identified_anime WHERE anidb_id=?', (anidb_id,)).fetchone()
        name = row[0].strip()[:80] if row else None
    notify(db, {'type': 'KEEP_SEARCH_FAILED',
                'title': f'Missing-episode search not started: {name}'[:120] if name else 'Missing-episode search not started',
                'message': 'Keep finished, but the library manager did not start the search for missing episodes. '
                           'Start a search for this series in the library manager.',
                'anidb_id': anidb_id, 'job_id': claim.job_id, 'severity': 'attention'}, now=now)


def _series_of_job_keep(db, job_id):
    row = db.execute(
        "SELECT json_extract(data_json,'$.selection.anidb_id') FROM keep_imports WHERE job_id=? "
        "UNION ALL SELECT json_extract(data_json,'$.selection.anidb_id') FROM batch_keep_imports WHERE job_id=? LIMIT 1",
        (job_id, job_id)).fetchone()
    return None if row is None else row[0]
