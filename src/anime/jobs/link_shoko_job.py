"""After every Keep: tell Shoko which AniDB episode each kept file of the series is.

Owner, 2026-11-23: Shoko's own hash match often fails, and Jellyfin (through
Shokofin) only shows files Shoko has linked to an episode. So every Keep is
followed by this job. It works on the whole series, not one Keep, so running it
again is harmless: files already linked to their episode are left alone, and a
Keep that never told Shoko (before this job existed) is caught up.

Each run: ask Shoko to fetch the series, then for each kept file find Shoko's
record of it (asking Shoko to scan its import folders if it hasn't seen the
file), turn the AniDB episode ID into Shoko's episode ID, and link them. A file
Shoko hasn't hashed yet, or an episode it hasn't fetched yet, is tried again in
WAIT_MS. After GIVE_UP_MS the job fails and says which files are not linked.
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
WAIT_MS = 600_000
GIVE_UP_MS = 24 * 3600 * 1000
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


def enqueue(db, anidb_id, *, claim=None):
    """Queue the Shoko link for a series, inside the caller's transaction.

    A link already waiting to run will see this Keep's files too, so no second one.
    """
    waiting = db.execute("SELECT job_id FROM jobs WHERE kind=? AND state IN ('PENDING','RETRY_WAIT') "
                         "AND json_extract(payload_json,'$.anidb_id')=?", (KIND, anidb_id)).fetchone()
    if waiting:
        return waiting[0]
    return enqueue_job(db, kind=KIND, payload={'anidb_id': anidb_id, 'since_ms': time.time_ns() // 1_000_000},
                       component='worker', event_id=None if claim is None else claim.event_id,
                       created_by_job_id=None if claim is None else claim.job_id)


def link_series(database, shoko, anidb_id):
    """Link what Shoko can link now. Returns (linked, waiting): counts and the files still waiting."""
    with closing(connect(database)) as db:
        files = kept_files(db, anidb_id)
    shoko.refresh_series(anidb_id)
    linked, waiting, scanned = 0, [], False
    for path, anidb_episode_id in files:
        found = shoko.find_file(path)
        if found is None:
            if not scanned:
                shoko.scan()
                scanned = True
            waiting.append(f'{path}: Shoko has not found the file yet')
            continue
        file_id, episodes = found
        if anidb_episode_id in episodes:
            continue
        episode = shoko.episode_id(anidb_episode_id)
        if episode is None:
            waiting.append(f'{path}: Shoko has not fetched AniDB episode {anidb_episode_id} yet')
            continue
        shoko.link(file_id, episode)
        LOG.info('Shoko: linked %s to AniDB episode %s (Shoko episode %s)', path, anidb_episode_id, episode)
        linked += 1
    return linked, waiting


def registration(secrets, *, client=Shoko):
    async def execute(context):
        payload = context.data()
        anidb_id, since = payload['anidb_id'], payload['since_ms']
        database = context.configuration.value('storage.database_path')
        try:
            shoko = client(context.configuration, secrets)
            linked, waiting = await blocking(link_series, database, shoko, anidb_id)
        except ShokoConfiguration:
            return Outcome('review', 'SERVICE_SETUP_INVALID', facts=service_facts('shoko', 'NOT_CONFIGURED'))
        except ShokoUnavailable:
            return Outcome('retry', 'SERVICE_UNAVAILABLE', facts=service_facts('shoko'))
        except ShokoRefused as exc:
            LOG.error('Shoko link for AniDB %s stopped: %s', anidb_id, exc)
            return Outcome('fail', exc.reason_code)
        if not waiting:
            LOG.info('Shoko: every kept file of AniDB %s is linked (%s new)', anidb_id, linked)
            return Outcome()
        if time.time_ns() // 1_000_000 - since > GIVE_UP_MS:
            LOG.error('Shoko link for AniDB %s gave up after a day; not linked:\n  %s', anidb_id,
                      '\n  '.join(waiting))
            return Outcome('fail', 'MEDIA_LINK_INCOMPLETE', facts={'failed_path': waiting[0][:500]})
        LOG.info('Shoko: AniDB %s waiting on %s file(s), checking again in %s min; first: %s',
                 anidb_id, len(waiting), WAIT_MS // 60000, waiting[0])
        return Outcome('defer', 'MEDIA_LINK_WAITING', delay_ms=WAIT_MS)
    return Registration(KIND, DEFINITION, execute, services=SERVICES)
