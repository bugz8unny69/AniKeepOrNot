"""SJ-4b: Reserve episodes in episodes_json."""
from src.anime.series_job.steps.common import StepResult
from src.anime.series_job.store import SeriesRow, note


async def reserve_episodes(series: SeriesRow, torrent: dict, context) -> StepResult:
    """Reserve torrent's episodes in series.episodes, ensuring no overlap with other torrents."""
    episodes = torrent.get("episodes", [])
    if not episodes:
        return StepResult.drop("NO_EPISODES_TO_RESERVE")

    infohash = torrent.get("infohash", "")
    release_name = torrent.get("release_name", "")

    # Check if all episodes are already staged or kept
    all_staged = True
    for ep_code in episodes:
        existing = series.episodes.get(ep_code)
        if not existing or existing.get("state") not in ("staged", "kept"):
            all_staged = False
            break

    if all_staged:
        note(series, f"series {series.anidb_id} {series.title} · {release_name} · dropped: episodes already staged")
        return StepResult.drop("EPISODE_ALREADY_STAGED")

    # Check if any episode is reserved or in-flight by ANOTHER torrent
    for ep_code in episodes:
        existing = series.episodes.get(ep_code)
        if existing and existing.get("torrent") != infohash:
            if existing.get("state") in ("reserved", "downloading", "transcoding"):
                other_hash = existing.get("torrent", "unknown")
                return StepResult.wait(until_ms=None, why=f"waiting for episode {ep_code} from torrent {other_hash}")

    # Reserve each episode for this torrent
    for ep_code in episodes:
        existing = series.episodes.get(ep_code)
        if not existing or existing.get("state") not in ("staged", "kept"):
            series.episodes[ep_code] = {
                "state": "reserved",
                "torrent": infohash,
                "file": None,
            }

    torrent["step"] = "download"
    note(series, f"series {series.anidb_id} {series.title} · {release_name} · reserved episodes: {', '.join(episodes)}")
    return StepResult.done(next_step="download")
