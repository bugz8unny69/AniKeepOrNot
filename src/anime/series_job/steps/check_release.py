"""SJ-4a: Check the release (single episode and season pack)."""
import json
import logging
from src.anime.batch.recognition import movie_title
from src.anime.release_contracts import ReleaseInput, VerifiedRelease, ReleaseNotReady, ReleaseConflict
from src.anime.batch.provider_worker import BatchVerificationCompletion
from src.anime.batch.selection import BatchPlanningError
from src.anime.acquisition.metadata import MetadataInvalid, SourceMissing
from src.anime.acquisition.settings import SizeLimitExceeded
from src.anime.acquisition.metainfo_source import MetadataSourceWaiting, MetadataSourceUnavailable
from src.anime.providers.errors import ProviderDeferred, ProviderUnavailable, ProviderInvalidResponse
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note

logger = logging.getLogger("akon.series")


async def check_release(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Verify release metadata and extract covered episodes."""
    release_name = torrent.get("release_name") or ""
    if movie_title(release_name):
        note(series, f"series {series.anidb_id} {series.title} · {release_name} · dropped: movie")
        return StepResult.drop("RELEASE_MOVIE_SKIPPED")

    raw_body = torrent.get("raw_body")
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    elif not raw_body:
        # Generate minimal raw_body from torrent metadata if available
        raw_body = json.dumps({"release_name": release_name,
                              "torrent_infohash": torrent.get("infohash", "")}).encode("utf-8")

    tvdb_series_id = getattr(context, "tvdb_series_id", None)
    if not tvdb_series_id and hasattr(context, "db") and context.db is not None:
        try:
            row = context.db.execute("SELECT tvdb_series_id FROM recognized_series WHERE anidb_id=?",
                                     (series.anidb_id,)).fetchone()
            if row and row[0]:
                tvdb_series_id = row[0]
        except Exception:
            pass
    if not tvdb_series_id:
        tvdb_series_id = series.anidb_id

    source = ReleaseInput(
        event_id=1,
        identity_result_id=1,
        anidb_id=series.anidb_id,
        tvdb_series_id=tvdb_series_id,
        raw_body=raw_body,
    )

    try:
        config = getattr(context, "configuration", None)
        cancelled = getattr(context, "cancelled", lambda: False)
        verified = await context.verify(source, config, cancelled)
    except MetadataSourceWaiting as exc:
        delay = getattr(exc, "delay_ms", 5000) or 5000
        now = getattr(context, "now", 0)
        return StepResult.wait(now + delay, why=getattr(exc, "reason_code", "WAITING"))
    except ProviderDeferred as exc:
        delay = getattr(exc, "delay_ms", 5000) or 5000
        now = getattr(context, "now", 0)
        return StepResult.wait(now + delay, why="JOB_WAITING")
    except (MetadataSourceUnavailable, ProviderUnavailable, ProviderInvalidResponse) as exc:
        now = getattr(context, "now", 0)
        return StepResult.wait(now + 60000, why=getattr(exc, "reason_code", "SERVICE_UNAVAILABLE"))
    except (ReleaseNotReady, SizeLimitExceeded, ReleaseConflict, MetadataInvalid, SourceMissing, BatchPlanningError) as exc:
        reason = getattr(exc, "reason_code", "RELEASE_INVALID")
        note(series, f"series {series.anidb_id} {series.title} · {release_name} · dropped: {reason}")
        return StepResult.drop(reason)

    if isinstance(verified, VerifiedRelease):
        season = verified.tvdb_season or 1
        ep_num = verified.tvdb_episode or verified.anidb_episode_number
        ep_code = f"S{season:02d}E{ep_num:02d}"
        torrent["episodes"] = [ep_code]
        torrent["step"] = "reserve_episodes"
        note(series, f"series {series.anidb_id} {series.title} · {ep_code} {release_name} · check release: single episode {ep_code}")
        return StepResult.done(next_step="reserve_episodes")

    if isinstance(verified, BatchVerificationCompletion):
        episodes = []
        if hasattr(context, "propose_batch"):
            episodes = await context.propose_batch(verified, context)
        else:
            for ep in getattr(verified.verified, "episodes", []):
                s = getattr(ep, "tvdb_season", 1) or 1
                n = getattr(ep, "tvdb_episode", None) or getattr(ep, "anidb_episode_number", 1)
                episodes.append(f"S{s:02d}E{n:02d}")
        if not episodes:
            episodes = ["S01E01"]
        torrent["episodes"] = episodes
        torrent["step"] = "reserve_episodes"
        note(series, f"series {series.anidb_id} {series.title} · {release_name} · check release: season pack with {len(episodes)} episodes")
        return StepResult.done(next_step="reserve_episodes")

    # If verification returned something else or custom batch completion
    episodes = getattr(verified, "episodes", ["S01E01"])
    torrent["episodes"] = episodes
    torrent["step"] = "reserve_episodes"
    return StepResult.done(next_step="reserve_episodes")
