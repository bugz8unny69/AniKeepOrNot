"""Tell Shoko which AniDB episode each kept file is (owner, 2026-11-23); part of Keep.

Shoko's own hash match often fails, and Jellyfin (through Shokofin) only shows
files Shoko has linked to an episode. AKON knows the AniDB episode of every kept
file, so it tells Shoko. Shoko 5.3 can only link a file it has recorded (it
records it when it hashes it), so AKON asks it to import new files when one is
missing, and Keep checks again until a deadline (series_job/keep.py).
"""
import logging

LOG = logging.getLogger('akon.series')


def link_files(shoko, anidb_id, files):
    """link_series for a given list of (path relative to the library, AniDB episode ID); the series job's Keep."""
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
