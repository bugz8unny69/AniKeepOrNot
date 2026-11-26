"""Check the release: which episodes it is, and which of its files to download.

Runs after the metadata step, with the real release verifier (providers/
releases.py; season packs through batch/provider_worker.py, which reads the
saved .torrent bytes). The identity comes from the API's identity process,
stored on the torrent (torrent["identity"]).

- A single episode: its episode and the torrent's one video file.
- A season pack: every verified video, and the trial window from its first
  episode (Settings: backfill and following counts; owner, 2026-11-26: a pack
  starting at E13 starts its trial at E13).

Each chosen file is kept with its AniDB episode ID (Keep and Shoko need it).
"""
from src.anime.acquisition.media import MediaInvalid
from src.anime.acquisition.metadata import MetadataInvalid, SourceMissing, parse_metainfo
from src.anime.acquisition.settings import SizeLimitExceeded
from src.anime.batch.provider_worker import BatchVerificationCompletion
from src.anime.batch.recognition import movie_title
from src.anime.batch.selection import BatchPlanningError
from src.anime.providers.errors import (ProviderAuthenticationFailed, ProviderConfigurationError, ProviderDeferred,
                                        ProviderInvalidResponse, ProviderUnavailable)
from src.anime.release_contracts import ReleaseConflict, ReleaseInput, ReleaseNotReady, VerifiedRelease
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS, StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import read_metainfo
from src.anime.user_evaluation_policy import Window


def code(season, episode):
    return f"S{season:02d}E{episode:02d}"


async def check_release(series: SeriesRow, torrent: dict, context) -> StepResult:
    configuration, now = context.configuration, context.now
    release_name = torrent.get("release_name") or ""
    label = f"series {series.anidb_id} {series.title} · {release_name}"
    if movie_title(release_name):
        note(series, f"{label} · dropped: movie")
        return StepResult.drop("RELEASE_MOVIE_SKIPPED")
    raw = read_metainfo(configuration, torrent["infohash"])
    if raw is None:
        return StepResult.done(next_step="get_metadata")         # The file went missing: read it again.

    # The release verifier's IDs name the old event rows; the series has none, so it gets
    # the AniDB ID (positive, and not used to look anything up on this path).
    source = ReleaseInput(event_id=series.anidb_id, identity_result_id=series.anidb_id,
                          anidb_id=series.anidb_id,
                          tvdb_series_id=torrent["identity"]["anime"]["tvdb_series_id"],
                          raw_body=torrent["raw_body"].encode())
    try:
        verified = await context.verify(source, configuration, context.cancelled)
    except ProviderDeferred as exc:
        return StepResult.wait(now + exc.delay_ms, why="JOB_WAITING")              # AniDB/TVDB pacing.
    except (ProviderUnavailable, ProviderInvalidResponse) as exc:
        return StepResult.wait(now + SERVICE_WAIT_MS, why=exc.reason_code)
    except (ProviderAuthenticationFailed, ProviderConfigurationError) as exc:
        return StepResult.wait(now + SETUP_WAIT_MS, why=exc.reason_code)
    except (ReleaseNotReady, ReleaseConflict, SizeLimitExceeded, MetadataInvalid, SourceMissing,
            MediaInvalid) as exc:
        note(series, f"{label} · dropped: {exc.reason_code}")
        return StepResult.drop(exc.reason_code)
    except BatchPlanningError as exc:                     # Carries a message, no reason code.
        note(series, f"{label} · dropped: the pack's files don't match its episodes ({exc})")
        return StepResult.drop(f"BATCH_PLANNING: {exc}"[:200])

    if type(verified) is VerifiedRelease:
        try:
            manifest = parse_metainfo(raw, torrent["infohash"])
        except MetadataInvalid as exc:
            note(series, f"{label} · dropped: not one video ({exc.reason_code})")
            return StepResult.drop(exc.reason_code)
        season = verified.tvdb_season or 1
        episode = verified.tvdb_episode or verified.anidb_episode_number
        video = manifest.files[manifest.selected_index]
        torrent["files"] = [{"index": video.index, "path": video.path, "size": video.size,
                             "episode": code(season, episode),
                             "anidb_episode_id": verified.anidb_episode_id,
                             "tvdb_episode_id": verified.tvdb_episode_id}]
    elif type(verified) is BatchVerificationCompletion:
        window = Window(configuration.value("user_evaluation.backfill_count"),
                        configuration.value("user_evaluation.following_count"))
        facts = verified.verified.files
        trial = set(window.pack_episodes(min(f.tvdb_episode for f in facts)))
        torrent["files"] = [{"index": f.index, "path": f.path, "size": verified.inventory.files[f.index].size,
                             "episode": code(f.tvdb_season, f.tvdb_episode),
                             "anidb_episode_id": f.anidb_episode_id, "tvdb_episode_id": f.tvdb_episode_id}
                            for f in sorted(facts, key=lambda f: f.tvdb_episode) if f.tvdb_episode in trial]
    else:
        raise TypeError(f"release verifier returned {type(verified).__name__}")
    torrent["episodes"] = [f["episode"] for f in torrent["files"]]
    note(series, f"{label} · release checked: {', '.join(torrent['episodes'])}")
    return StepResult.done(next_step="reserve_episodes")
