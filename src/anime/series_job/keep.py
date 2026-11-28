"""Keep, as series work: Sonarr owns the series, then Shoko, dubs, Jellyfin and cleanup.

The Keep action records series.work["keep"] and wakes the series; the worker
runs these steps (one per attempt) instead of torrent steps, then the series is
KEPT. They replace the old KEEP_MEDIA/KEEP_BATCH, LINK_SHOKO,
FIND_DUBS_FOR_SONARR, REFRESH_JELLYFIN and cleanup jobs (owner, 2026-11-26:
no jobs table after the cutover).

1. cancel          drop the torrents still working (not staged).
2. sonarr_series   add the series to Sonarr once; recorded before the send, a
                   lost reply is matched by TVDB ID, never resent.
3. sonarr_import   per staged episode, as keep_service did: copy import
                   recorded before it is sent, then Sonarr's file is checked.
4. search          one Sonarr search for the missing episodes; never fails Keep.
5. shoko           tell Shoko each kept file's AniDB episode (when set up).
6. dubs            AKON finds dubs for Sonarr's missing episodes (audio check
                   japanese_english only), DUBS_DELAY_MS after the import.
7. jellyfin        rescan (when set up).
8. cleanup         remove AKON's torrents and the staged copies.

Sonarr refusing something stops the Keep with why (work["keep"]["stopped"]) and
tells you; Retry resumes it. A service down or not set up is a wait.
"""
from dataclasses import asdict
from pathlib import PurePosixPath
import re

from src.anime.storage_mounts import LIBRARY, STAGING, uses_storage
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS, StepResult
from src.anime.series_job.store import note

STEPS = ("cancel", "sonarr_series", "sonarr_import", "search", "shoko", "dubs", "jellyfin", "cleanup")
POLL_MS = 10_000
# Sonarr marks a ManualImport completed even when it imported nothing (its log says why,
# e.g. no write access to its library folder). Past this, Keep stops and says so.
IMPORT_SETTLE_MS = 5 * 60_000
DUBS_DELAY_MS = 10 * 60_000
SHOKO_WAIT_MS = 30_000
SHOKO_FILE_DEADLINE_MS = 30 * 60_000
SHOKO_EPISODE_DEADLINE_MS = 2 * 3600_000


def request(series, now):
    """The Keep action: Sonarr owns this series from now on."""
    series.work["keep"] = {"requested_at_ms": now, "step": "cancel", "imports": {}}
    series.state, series.wake_at_ms = "WORKING", None


def _selection(configuration, series, code, episode):
    from src.anime.series_job.keep_selection import KeepSelection
    torrent = next(t for t in series.torrents if t["infohash"] == episode["torrent"])
    entry = next(f for f in torrent["files"] if f["episode"] == code)
    return KeepSelection(anidb_id=series.anidb_id, anidb_episode_id=entry["anidb_episode_id"],
                         tvdb_series_id=torrent["identity"]["anime"]["tvdb_series_id"],
                         tvdb_episode_id=entry["tvdb_episode_id"], tvdb_season=int(code[1:3]),
                         tvdb_episode=int(code[4:]), preparation_plan_id=series.anidb_id,
                         staging_root=configuration.value("storage.user_evaluation_root"),
                         media_relative_path=episode["file"]), entry


def _staged(series):
    return {code: ep for code, ep in sorted(series.episodes.items()) if ep.get("state") in ("staged", "kept")}


def _highest_episode(configuration, series):
    from contextlib import closing
    from src.anime.providers.errors import ProviderInvalidResponse
    from src.anime.providers.parsing import episode_catalog
    from src.anime.storage.sqlite import connect
    with closing(connect(configuration.value("storage.database_path"))) as db:
        row = db.execute("SELECT payload FROM provider_public_cache WHERE provider='anidb' AND cache_key=?",
                         (f"anime:{series.anidb_id}",)).fetchone()
    if row is not None:
        try:
            catalog = episode_catalog(row[0], series.anidb_id)
            if catalog:
                return max(catalog)
        except (ProviderInvalidResponse, ValueError):
            pass
    numbers = [int(code[4:]) for code in series.episodes]
    return max(numbers) if numbers else None


def _sonarr(context, series):
    from src.anime.sonarr import Client, Options
    staged = _staged(series)
    code, episode = next(iter(staged.items()))
    selection, _ = _selection(context.configuration, series, code, episode)
    options = Options.capture(context.configuration, selection)
    return Client(options, context.secrets.sonarr_api_key()), options


def sonarr_series(series, keep, context):
    from src.anime.checks import here
    from src.anime.sonarr import KeepReview
    from src.anime.series_job.keep_selection import monitor_mode
    client, options = _sonarr(context, series)
    version = client.request("system/status")
    if not re.match(r"^4\.", str(version.get("version", ""))):
        raise KeepReview(failed_check=here("VERSION_UNSUPPORTED"))
    tvdb = next(iter(t["identity"]["anime"]["tvdb_series_id"] for t in series.torrents if t.get("identity")))
    state = keep.setdefault("sonarr", {"phase": "PLANNED"})
    found = client.series(tvdb)
    if found is None:
        if state["phase"] == "ADD_UNCERTAIN":
            raise KeepReview(failed_check=here("SERIES_ADD_UNCONFIRMED"))   # Sent, and still not there.
        state["monitor"] = monitor_mode(_highest_episode(context.configuration, series), options)
        state["phase"] = "ADD_UNCERTAIN"
        context.checkpoint()                       # Recorded before the POST: never resent.
        try:
            added = client.request("series", body=client.add_body(tvdb, state["monitor"]))
        except KeepReview:
            state["phase"] = "PLANNED"             # Sonarr answered and refused: nothing was added.
            raise
        found = client.series(tvdb)
        if found is None or found.get("id") != added.get("id"):
            raise KeepReview(failed_check=here("SERIES_ADDED_NOT_FOUND"))
    path = PurePosixPath(found["path"])
    if (type(found.get("id")) is not int or not path.is_relative_to(options.library_root)
            or path == PurePosixPath(options.library_root) or str(path) != found["path"]):
        raise KeepReview(failed_check=here("SERIES_PATH_OUTSIDE_LIBRARY"))
    state.update(phase="READY", series_id=found["id"], series_path=found["path"], tvdb_series_id=tvdb)
    note(series, f"series {series.anidb_id} {series.title} · Sonarr has the series ({found['path']})")
    return "sonarr_import"


def sonarr_import(series, keep, context):
    """One step of each staged episode's import; 'search' once all are in Sonarr."""
    from src.anime.checks import here
    from src.anime.sonarr import KeepReview, existing_file
    from src.anime.series_job.keep_selection import check_source
    client, options = _sonarr(context, series)
    state = keep["sonarr"]
    found = client.series(state["tvdb_series_id"])
    if found is None or (found.get("id"), found.get("path")) != (state["series_id"], state["series_path"]):
        raise KeepReview(failed_check=here("SERIES_RECONCILIATION_MISMATCH"))
    waiting = False
    for code, ep in _staged(series).items():
        record = keep["imports"].setdefault(code, {"phase": "PLANNED"})
        if record["phase"] == "COMPLETE":
            continue
        selection, entry = _selection(context.configuration, series, code, ep)
        episode = client.episode(selection, found)
        if record["phase"] == "PLANNED":
            if episode.get("episodeFileId", 0):
                raise KeepReview(failed_check=here("EPISODE_ALREADY_HAS_FILE"))
            # The staged copy must still be the one staging wrote (its size and time).
            data = {"selection": asdict(selection), "source_file": entry["staged_file"]}
            check_source(data)
            # Still the copy staging wrote: its size and content (never its mtime, owner 2026-11-27).
            from src.anime.acquisition.media import same_content
            if not same_content(selection.staging_root, selection.media_relative_path, entry["staged_file"][2],
                                entry.get("staged_content")):
                raise KeepReview(failed_check=here("STAGED_FILE_CHANGED"))
            record.update(payload=client.import_payload(selection, found, episode), episode_id=episode["id"],
                          source_file=data["source_file"], phase="IMPORT_UNCERTAIN")
            context.checkpoint()                   # Recorded before the import command is sent.
            reply = client.request("command", body=record["payload"])
            if type(reply.get("id")) is not int or reply["id"] <= 0:
                raise KeepReview(failed_check=here("COMMAND_REJECTED"))
            record["command_id"] = reply["id"]
            waiting = True
            continue
        if episode["id"] != record["episode_id"]:
            raise KeepReview(failed_check=here("PHASE_OR_EPISODE_MISMATCH"))
        if "command_id" not in record:
            matches = [row for row in client.request("command")
                       if str(row.get("body", {}).get("name", "")).lower() == "manualimport"
                       and row.get("body", {}).get("files", [{}])[:1]
                       and all(row["body"]["files"][0].get(k) == record["payload"]["files"][0][k]
                               for k in ("path", "seriesId", "episodeIds"))]
            if not matches:
                waiting = True
                continue
            if len(matches) != 1:
                raise KeepReview(failed_check=here("IMPORT_COMMAND_AMBIGUOUS"))
            record["command_id"] = matches[0]["id"]
        status = str(client.request("command/" + str(record["command_id"])).get("status", "")).lower()
        if status in ("failed", "aborted", "cancelled"):
            raise KeepReview(failed_check=here("COMMAND_FAILED"))
        episode = client.episode(selection, found)
        if status == "completed" and not (episode.get("hasFile") and episode.get("episodeFileId")):
            seen = record.setdefault("completed_without_file_ms", context.now)
            if context.now - seen >= IMPORT_SETTLE_MS:
                record.clear()
                record["phase"] = "PLANNED"              # Retry sends the import again.
                raise KeepReview(f"Sonarr finished importing {code} but has no file for it; Sonarr's log "
                                 "says why (often no write access to its library folder)",
                                 failed_check=here("IMPORT_NOT_DONE"))
            waiting = True
            continue
        if status != "completed":
            waiting = True
            continue
        file_record = client.request("episodefile/" + str(episode["episodeFileId"]))
        try:
            record["result"] = existing_file(options, found, episode, file_record, record["source_file"][2])
        except FileNotFoundError:
            waiting = True
            continue
        record["phase"] = "COMPLETE"
        series.episodes[code]["state"] = "kept"
        note(series, f"series {series.anidb_id} {series.title} · {code} kept: {record['result']['relative_path']}")
    return None if waiting else "search"


def search(series, keep, context):
    from src.anime.sonarr import KeepReview, KeepUnavailable
    if "search" not in keep:
        keep["search"] = "SENDING"
        context.checkpoint()                       # Recorded before it is sent: one search per Keep.
        client, _ = _sonarr(context, series)
        try:
            keep["search"] = "SENT" if client.search(keep["sonarr"]["series_id"]) else "NOTHING_MISSING"
        except (KeepReview, KeepUnavailable):
            keep["search"] = "FAILED"              # Never fails the Keep; Sonarr searches on its own too.
        note(series, f"series {series.anidb_id} {series.title} · Sonarr search for missing episodes: "
                     f"{keep['search'].lower().replace('_', ' ')}")
    return "shoko"


def kept_files(series, still_there=None):
    """(path in the library, AniDB episode ID) of every file Keep imported.

    still_there: {Sonarr episode ID: its episode file ID now}. A file Sonarr has since
    replaced (an upgrade) is left out: it is gone, and Sonarr's new file is Shoko's own
    to find (owner, 2026-11-28: Shoko waited 30 minutes for files Sonarr had deleted)."""
    return [(record["result"]["relative_path"], next(
        f["anidb_episode_id"] for t in series.torrents for f in t.get("files", []) if f.get("episode") == code
        and t["infohash"] == series.episodes[code]["torrent"]))
        for code, record in sorted(series.work["keep"]["imports"].items()) if record.get("phase") == "COMPLETE"
        and (still_there is None or still_there.get(record["episode_id"]) == record["result"]["episode_file_id"])]


def sonarr_files(context, series):
    """{Sonarr episode ID: episode file ID} for the kept series, as Sonarr has them now."""
    client, _ = _sonarr(context, series)
    rows = client.request("episode", query={"seriesId": series.work["keep"]["sonarr"]["series_id"]})
    return {row.get("id"): row.get("episodeFileId") for row in rows or ()}


def shoko(series, keep, context):
    from src.anime.series_job.shoko_link import link_files
    from src.anime.shoko import Shoko
    if not context.configuration.value("shoko.base_url"):
        return "dubs"
    files = kept_files(series, sonarr_files(context, series))
    replaced = len(kept_files(series)) - len(files)
    if replaced and not keep.get("shoko", {}).get("replaced"):
        keep.setdefault("shoko", {"since_ms": context.now})["replaced"] = replaced
        note(series, f"series {series.anidb_id} {series.title} · Shoko: {replaced} kept file(s) Sonarr has since "
                     "replaced (upgrades) are left to Shoko to find")
    linked, no_file, no_episode = link_files(Shoko(context.configuration, context.secrets), series.anidb_id, files)
    state = keep.setdefault("shoko", {"since_ms": context.now})
    state["linked"] = state.get("linked", 0) + linked
    if not no_file and not no_episode:
        note(series, f"series {series.anidb_id} {series.title} · Shoko: every kept file is linked")
        return "dubs"
    waited = context.now - state["since_ms"]
    if (no_file and waited >= SHOKO_FILE_DEADLINE_MS) or (no_episode and waited >= SHOKO_EPISODE_DEADLINE_MS):
        note(series, f"series {series.anidb_id} {series.title} · Shoko could not link: "
                     f"{', '.join(no_file + no_episode)}; check Shoko's mount of the library and its AniDB login")
        keep["shoko"]["incomplete"] = no_file + no_episode
        return "dubs"                              # Keep still finishes; you are told what is unlinked.
    return None


def dubs(series, keep, context):
    from src.anime.acquisition.audio import wants_english
    if not wants_english(context.configuration.value("acquisition.audio_check")):
        return "jellyfin"
    if context.now < keep["requested_at_ms"] + DUBS_DELAY_MS:
        return None                                # Sonarr's own search goes first.
    grabbed, missing = find_dubs_now(series, keep["sonarr"]["tvdb_series_id"], context.configuration, context.secrets)
    keep["dubs"] = {"grabbed": grabbed, "missing": missing}
    return "jellyfin"


def find_dubs_now(series, tvdb, configuration, secrets):
    """AKON's dub search for Sonarr's missing episodes of a kept series (also adm sonarr find-dubs)."""
    from src.anime.series_job.dubs import find_and_push, mappings
    from src.anime.acquisition.prowlarr_search import Client as Prowlarr
    from src.anime.providers.backfill_catalog import make_catalog_factory
    from src.anime.providers.configuration import episode_placement_credentials, options_from_configuration
    from src.anime.providers.identity import ClientPool
    from src.anime.sonarr import api_client
    from functools import partial
    pool = ClientPool()
    catalog = make_catalog_factory(options_from_configuration, partial(episode_placement_credentials, secrets),
                                   client_pool=pool)(configuration)
    prowlarr = Prowlarr.from_context(configuration, secrets, catalog=catalog)
    prowlarr.set_context(anidb_id=series.anidb_id, tvdb_series_id=tvdb, search_mode="sonarr_missing")
    grabbed, missing = find_and_push(api_client(configuration, secrets.sonarr_api_key()), prowlarr,
                                     catalog, series.anidb_id, tvdb, mappings(pool, configuration, series.anidb_id, tvdb))
    note(series, f"series {series.anidb_id} {series.title} · dub search: Sonarr grabbed {len(grabbed)}"
                 + (f"; nothing usable for {', '.join(missing)}" if missing else ""))
    return grabbed, missing


def jellyfin(series, keep, context):
    from src.anime.jellyfin import request_refresh
    if context.configuration.value("jellyfin.base_url"):
        request_refresh(context.configuration, context.secrets)
        note(series, f"series {series.anidb_id} {series.title} · Jellyfin asked to rescan")
    return "cleanup"


def cleanup(series, keep, context):
    """Remove AKON's torrents (and their files) and the staged copies Sonarr imported."""
    for torrent in series.torrents:
        qb = torrent.get("qb") or {}
        if qb.get("added") and not torrent.get("removed"):
            prefix = qb.get("prefix", "akon")
            client = (context.torrents.client(context.configuration, qb["marker"], prefix=prefix)
                      if prefix != "akon" else context.torrents.client(context.configuration, qb["marker"]))
            client.remove_series_torrent(torrent["infohash"], qb["marker"])
            torrent["removed"] = True
    # The staged copies Sonarr imported, with their NFOs; tvshow.nfo and the folders once no
    # staged episode is left. Same rule as Reject: a path AKON planned, the same content.
    from src.anime.series_job.reject import remove_staged
    imported = {code for code, record in keep["imports"].items() if record.get("phase") == "COMPLETE"}
    left = {code for code, ep in series.episodes.items() if ep.get("state") == "staged"} - imported
    differ = sum(remove_staged(context.configuration, torrent, imported, series_nfo=not left)
                 for torrent in series.torrents)
    # An imported episode with no staging plan behind it: its recorded path, by the same rule.
    import os
    from src.anime.acquisition.cleanup import directory, parent
    from src.anime.acquisition.media import same_content
    planned = {code for t in series.torrents for code in ((t.get("staging") or {}).get("plans") or {})}
    root = context.configuration.value("storage.user_evaluation_root")
    for code in sorted(imported - planned):
        relative = series.episodes[code]["file"]
        entry = next((f for t in series.torrents for f in t.get("files", []) if f.get("episode") == code
                      and f.get("staged_file")), None)
        if entry is None:
            continue
        ours = same_content(root, relative, entry["staged_file"][2], entry.get("staged_content"))
        if ours:
            with directory(root) as base, parent(base, relative) as (folder, name):
                try:
                    os.unlink(name, dir_fd=folder)
                except FileNotFoundError:
                    pass
        elif ours is False:
            differ += 1
    if differ:
        note(series, f"series {series.anidb_id} {series.title} · {differ} staged file(s) differ from what AKON "
                     "wrote, so they were left in the staging folder")
    note(series, f"series {series.anidb_id} {series.title} · kept; Sonarr owns it")
    return "done"


HANDLERS = {"sonarr_series": sonarr_series, "sonarr_import": sonarr_import, "search": search, "shoko": shoko,
            "dubs": dubs, "jellyfin": jellyfin, "cleanup": cleanup}


@uses_storage(STAGING, LIBRARY)
async def run_keep(series, context):
    """One Keep step. Returns a StepResult for the worker (done: the series is KEPT)."""
    from src.anime.async_io import blocking
    from src.anime.jellyfin import RefreshConfiguration, RefreshReview, RefreshUnavailable
    from src.anime.series_job.steps.drop import drop_torrent
    from src.anime.shoko import ShokoConfiguration, ShokoRefused, ShokoUnavailable
    from src.anime.sonarr import KeepConfiguration, KeepReview, KeepUnavailable
    from src.anime.acquisition.submission import ClientUnavailable
    keep = series.work["keep"]
    if keep.get("stopped"):
        return StepResult.wait(None, why="KEEP_IMPORT_REVIEW")
    while True:
        step = keep["step"]
        if step == "done":
            return StepResult.done(next_step="done")
        if step == "cancel":
            for torrent in series.torrents:
                if torrent.get("state") in ("waiting", "working", "stopped"):
                    await drop_torrent(series, torrent, "series kept", context)
            keep["step"] = "sonarr_series"
            continue
        try:
            following = await blocking(HANDLERS[step], series, keep, context)
        except (KeepConfiguration, ShokoConfiguration, RefreshConfiguration):
            return StepResult.wait(context.now + SETUP_WAIT_MS, why="SERVICE_SETUP_INVALID")
        except (KeepUnavailable, ShokoUnavailable, RefreshUnavailable, ClientUnavailable):
            return StepResult.wait(context.now + SERVICE_WAIT_MS, why="SERVICE_UNAVAILABLE")
        except (KeepReview, ShokoRefused, RefreshReview) as exc:
            where = getattr(exc, "failed_check", None)
            keep["stopped"] = f"{exc.reason_code}{f' at {where}' if where else ''}: {exc}"[:500]
            note(series, f"series {series.anidb_id} {series.title} · Keep stopped at {step}: {keep['stopped']}; "
                         "fix it, then Retry")
            return StepResult.wait(None, why=exc.reason_code)
        if following is None:
            wait = SHOKO_WAIT_MS if step == "shoko" else POLL_MS
            if step == "dubs":
                wait = keep["requested_at_ms"] + DUBS_DELAY_MS - context.now
            return StepResult.wait(context.now + max(wait, 1000), why="JOB_WAITING")
        keep["step"] = following
