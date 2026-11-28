"""Backfill search, as series work: find releases for the trial episodes no torrent covers yet.

Replaces DISCOVER_EPISODE_RELEASES. When the release check identifies a single
episode, the series wants its trial window (Settings: backfill and following
counts; Window.anidb_episodes). Each due attempt searches Prowlarr (AKON's
rules, four episodes per call) for the wanted AniDB episodes that no live
torrent covers and no staged or kept episode has, and adds each release found
as a torrent of the series. A release already tried for an episode is not
tried again. Episodes with nothing found are searched again every RETRY_MS,
until Keep or Reject; the Search action makes it due now.

series.work["search"] = {"wanted": [AniDB episode numbers], "due_ms": int,
                         "tried": {number: [info hashes]}}
"""
import json

from src.anime.series_job.store import note

PAGE = 4
RETRY_MS = 6 * 3600_000


def want(series, numbers, now):
    """The release check found numbers' anchor: add the trial window and make the search due."""
    search = series.work.setdefault("search", {"wanted": [], "tried": {}})
    search["wanted"] = sorted(set(search["wanted"]) | set(numbers))
    search["due_ms"] = now


def covered(series):
    """AniDB episode numbers a live torrent covers, or that are staged or kept."""
    numbers = set()
    for torrent in series.torrents:
        if torrent.get("state") in ("dropped",):
            continue
        for entry in torrent.get("files", []):
            if "anidb_episode_number" in entry:
                numbers.add(entry["anidb_episode_number"])
    return numbers


def run(series, context, prowlarr):
    """One search (blocking; in a thread). Returns how many torrents were added."""
    search = series.work["search"]
    tvdb = next(t["identity"]["anime"]["tvdb_series_id"] for t in series.torrents if t.get("identity"))
    identity = next(t["identity"] for t in series.torrents if t.get("identity"))
    missing = [n for n in search["wanted"] if n not in covered(series)]
    known = {t["infohash"] for t in series.torrents}
    added = []
    for start in range(0, len(missing), PAGE):
        page = tuple(missing[start:start + PAGE])
        tried = {n: set(search["tried"].get(str(n), ())) | known for n in page}
        for found in prowlarr.backfill(series.anidb_id, tvdb, page, select_candidates=True, tried_by_episode=tried):
            release = None if found.selection is None else found.selection.release
            if release is None or not prowlarr.policy.accepts(release) or release.info_hash in known:
                continue
            search["tried"].setdefault(str(found.number), []).append(release.info_hash)
            known.add(release.info_hash)
            body = {"release_name": release.title, "torrent_infohash": release.info_hash}
            if release.magnet_url:
                body["magnet_uri"] = release.magnet_url
            series.torrents.append({"infohash": release.info_hash, "release_name": release.title,
                                    "raw_body": json.dumps(body, sort_keys=True), "step": "get_metadata",
                                    "state": "waiting", "why": None, "episodes": [], "files": [],
                                    "progress": 0.0, "identity": identity, "found_for": found.number})
            added.append(f"E{found.number:02d} {release.title}")
    still = [n for n in missing if n not in {t.get("found_for") for t in series.torrents}]
    search["due_ms"] = context.now + RETRY_MS if still else None
    note(series, f"series {series.anidb_id} {series.title} · search: "
                 + (f"found {', '.join(added)}" if added else "nothing new")
                 + (f"; searching again later for E{', E'.join(f'{n:02d}' for n in still)}" if still else ""))
    return len(added)


async def run_search(series, context):
    """Run the search when due. Returns the time to wake for the next search, or None."""
    from src.anime.acquisition.prowlarr_search import (Client as Prowlarr, SearchConfiguration, SearchReview,
                                                       SearchUnavailable)
    from src.anime.async_io import blocking
    from src.anime.providers.errors import ProviderDeferred, ProviderUnavailable
    from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS
    search = series.work.get("search")
    if not search or search.get("due_ms") is None:
        return None
    if search["due_ms"] > context.now:
        return search["due_ms"]
    try:
        catalog = context.catalogs(context.configuration) if context.catalogs else None
        prowlarr = Prowlarr.from_context(context.configuration, context.secrets, catalog=catalog)
        await blocking(run, series, context, prowlarr)
    except SearchConfiguration:
        search["due_ms"] = context.now + SETUP_WAIT_MS
    except (SearchUnavailable, ProviderUnavailable):
        search["due_ms"] = context.now + SERVICE_WAIT_MS
    except ProviderDeferred as exc:                      # AniDB/TVDB pacing for episode placement.
        search["due_ms"] = context.now + exc.delay_ms
    except SearchReview as exc:
        note(series, f"series {series.anidb_id} {series.title} · search failed: {exc}; searching again later")
        search["due_ms"] = context.now + RETRY_MS
    return search["due_ms"]
