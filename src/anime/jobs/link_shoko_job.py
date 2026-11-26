"""Every Keep tells Shoko which AniDB episode each kept file is (owner, 2026-11-23).

Shoko's own hash match often fails, and Jellyfin (through Shokofin) only shows
files Shoko has linked to an episode. AKON knows the AniDB episode of every kept
file, so it tells Shoko instead of waiting for Shoko to work it out.

Shoko 5.3 can only link a file it has a record of, and it makes that record when
it hashes the file; no call registers a file by path. So:

1. Before Sonarr copies anything, Keep asks Shoko to fetch the series
   (prepare_series), so its episodes are there when the files land.
2. After the Keep, this job asks Shoko to import new files now (Shoko runs on
   another VM and reads the library over NFS, so its folder watcher can miss
   them), then links each kept file to its episode as soon as Shoko has it,
   checking every WAIT_MS.
3. It fails only when Shoko can't do it: a file still not found FILE_DEADLINE_MS
   after Shoko first answered (Shoko's mount of the library is the likely cause),
   or an episode Shoko still hasn't fetched after EPISODE_DEADLINE_MS. While
   Shoko is down it waits; that time doesn't count.

The job works on the whole series: a file already linked to its episode is left
alone, so it can run any number of times (adm shoko link --id <anidb_id>).
When every file is linked it queues a Jellyfin rescan (Jellyfin reads Shoko's VFS).
"""
from contextlib import closing
import logging
import time

from src.anime.async_io import blocking
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Outcome, Registration
from src.anime.services.base import service_facts
from src.anime.shoko import Shoko, ShokoConfiguration, ShokoRefused, ShokoUnavailable
from src.anime.storage.sqlite import connect, enqueue_job

KIND = 'LINK_SHOKO'
SERVICES = ('shoko',)
DEFINITION = HandlerDefinition(KIND, 'Link kept files to their episodes in Shoko')
WAIT_MS = 30_000
FILE_DEADLINE_MS = 30 * 60_000
EPISODE_DEADLINE_MS = 2 * 3600_000
WAITING = 'MEDIA_LINK_WAITING'
LOG = logging.getLogger('anime.worker')


def kept_files(db, anidb_id):
    """(path relative to the library, AniDB episode ID) of every kept file of the series."""
    import json
    files = []
    for (raw,) in db.execute("SELECT data_json FROM keep_imports WHERE phase='COMPLETE'"):
        data = json.loads(raw)
        if data['selection']['anidb_id'] == anidb_id:
            files.append((data['result']['relative_path'], data['selection']['anidb_episode_id']))
    files += db.execute(
        "SELECT json_extract(k.data_json,'$.result.relative_path'),r.anidb_episode_id FROM batch_keep_imports k "
        "JOIN batch_episode_reservations r ON r.plan_id=k.plan_id AND r.file_index=k.file_index "
        "WHERE k.phase='COMPLETE' AND r.anidb_id=? ORDER BY k.job_id,k.file_index", (anidb_id,)).fetchall()
    return files


def prepare_series(configuration, secrets, database, event_id, *, client=Shoko):
    """At the start of a Keep: Shoko fetches the series, so its episodes exist when the files land.

    Never holds up the Keep: if Shoko is down or not set up, the link job does it later.
    """
    if not configuration.value('shoko.base_url') or event_id is None:
        return
    with closing(connect(database)) as db:
        row = db.execute('SELECT anidb_id FROM identity_results WHERE event_id=? LIMIT 1', (event_id,)).fetchone()
    if row is None:
        return
    try:
        client(configuration, secrets).refresh_series(row[0])
        LOG.info('Shoko: asked to fetch AniDB %s before the Keep imports its files', row[0])
    except (ShokoConfiguration, ShokoUnavailable, ShokoRefused) as exc:
        LOG.warning('Shoko could not be asked to fetch AniDB %s before the Keep (%s); '
                    'the Shoko link after the Keep asks again', row[0], exc)


def enqueue(db, anidb_id, *, claim=None):
    """Queue the Shoko link for a series, inside the caller's transaction.

    A link already waiting to run will see this Keep's files too, so no second one.
    """
    waiting = db.execute("SELECT job_id FROM jobs WHERE kind=? AND state IN ('PENDING','RETRY_WAIT') "
                         "AND json_extract(payload_json,'$.anidb_id')=?", (KIND, anidb_id)).fetchone()
    if waiting:
        return waiting[0]
    return enqueue_job(db, kind=KIND, payload={'anidb_id': anidb_id},
                       component='worker', event_id=None if claim is None else claim.event_id,
                       created_by_job_id=None if claim is None else claim.job_id)


def link_series(database, shoko, anidb_id):
    """Link what Shoko can link now: (linked count, files Shoko hasn't found, episodes it hasn't fetched)."""
    with closing(connect(database)) as db:
        files = kept_files(db, anidb_id)
    shoko.refresh_series(anidb_id)
    linked, no_file, no_episode, nudged = 0, [], [], False
    for path, anidb_episode_id in files:
        found = shoko.find_file(path)
        if found is None:
            if not nudged:
                shoko.import_new_files()
                nudged = True
            no_file.append(path)
            continue
        file_id, episodes = found
        if anidb_episode_id in episodes:
            continue
        episode = shoko.episode_id(anidb_episode_id)
        if episode is None:
            no_episode.append(f'{path} (AniDB episode {anidb_episode_id})')
            continue
        shoko.link(file_id, episode)
        LOG.info('Shoko: linked %s to AniDB episode %s', path, anidb_episode_id)
        linked += 1
    return linked, no_file, no_episode


def waiting_since(database, job_id, now):
    """When this job started waiting on Shoko, not counting time Shoko was down."""
    with closing(connect(database)) as db:
        row = db.execute(
            "SELECT min(occurred_at_ms) FROM job_history WHERE job_id=? AND event_code='DEFERRED' AND reason_code=? "
            "AND history_id>COALESCE((SELECT max(history_id) FROM job_history WHERE job_id=? "
            "AND event_code IN ('DEFERRED','RETRY_SCHEDULED') AND reason_code IS NOT ?),0)",
            (job_id, WAITING, job_id, WAITING)).fetchone()
    return now if row[0] is None else row[0]


def _persist(db, claim, value):
    # Jellyfin reads Shoko's VFS: rescan once the links are in (owner, 2026-11-23).
    # Keep's own rescan runs before Shoko is linked, so this one is the one that counts.
    if value['refresh']:
        from src.anime.jobs.refresh_jellyfin_job import enqueue
        enqueue(db, claim)
    return ()


def registration(secrets, *, client=Shoko):
    async def execute(context):
        anidb_id = context.data()['anidb_id']
        database = context.configuration.value('storage.database_path')
        try:
            shoko = client(context.configuration, secrets)
            linked, no_file, no_episode = await blocking(link_series, database, shoko, anidb_id)
        except ShokoConfiguration:
            return Outcome('review', 'SERVICE_SETUP_INVALID', facts=service_facts('shoko', 'NOT_CONFIGURED'))
        except ShokoUnavailable:
            return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('shoko'))
        except ShokoRefused as exc:
            LOG.error('Shoko link for AniDB %s stopped: %s', anidb_id, exc)
            return Outcome('fail', exc.reason_code)
        if not no_file and not no_episode:
            LOG.info('Shoko: every kept file of AniDB %s is linked (%s new); asking Jellyfin to rescan',
                     anidb_id, linked)
            return Outcome(value={'linked': linked,
                                  'refresh': bool(context.configuration.value('jellyfin.base_url'))})
        now = time.time_ns() // 1_000_000
        waited = now - await blocking(waiting_since, database, context.claim.job_id, now)
        if no_file and waited >= FILE_DEADLINE_MS:
            LOG.error('Shoko did not find these kept files in %s minutes. Shoko runs on its own VM: check its '
                      'mount of the library (NFS).\n  %s', FILE_DEADLINE_MS // 60000, '\n  '.join(no_file))
            return Outcome('fail', 'MEDIA_LINK_INCOMPLETE', facts={'failed_path': no_file[0][:500]})
        if no_episode and waited >= EPISODE_DEADLINE_MS:
            LOG.error('Shoko has not fetched these AniDB episodes in %s hours; check its AniDB login.\n  %s',
                      EPISODE_DEADLINE_MS // 3600000, '\n  '.join(no_episode))
            return Outcome('fail', 'MEDIA_LINK_INCOMPLETE', facts={'failed_path': no_episode[0][:500]})
        if waited == 0:
            LOG.info('Shoko: AniDB %s waiting for Shoko to record %s file(s) and fetch %s episode(s); '
                     'checking every %s s', anidb_id, len(no_file), len(no_episode), WAIT_MS // 1000)
        return Outcome('defer', WAITING, delay_ms=WAIT_MS)
    return Registration(KIND, DEFINITION, execute, services=SERVICES, persist=_persist)
