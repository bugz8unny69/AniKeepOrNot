"""Check each downloaded file: its size, its container and its audio (acquisition.audio_check).

The real checks (acquisition/media.py verify_path, ffprobe) on the file in the
torrent's own folder. A file that fails is dropped with why (wrong audio, not a
video); a file that can't be read yet (the share, ffprobe busy) waits.
"""
from src.anime.acquisition.media import MediaInvalid, MediaUnavailable, ProbeUnavailable, verify_path
from src.anime.async_io import blocking
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS, StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import options_for


async def check_media(series: SeriesRow, torrent: dict, context) -> StepResult:
    label = (f"series {series.anidb_id} {series.title} · {', '.join(torrent['episodes'])} "
             f"{torrent.get('release_name', '')}")
    root = options_for(context.configuration, torrent["qb"]["marker"]).download_root
    audio = context.configuration.value("acquisition.audio_check")
    for entry in torrent["files"]:
        if "media" in entry:
            continue
        try:
            entry["media"] = await blocking(verify_path, root, entry["path"], entry["size"], context.cancelled,
                                            audio=audio)
        except MediaUnavailable as exc:
            return StepResult.wait(context.now + SERVICE_WAIT_MS, why=exc.reason_code)
        except ProbeUnavailable as exc:
            return StepResult.wait(context.now + SETUP_WAIT_MS, why=exc.reason_code)
        except MediaInvalid as exc:                       # Includes AudioMissing.
            note(series, f"{label} · dropped: {entry['episode']} failed its media check ({exc.reason_code})")
            return StepResult.drop(exc.reason_code)
    for entry in torrent["files"]:
        series.episodes[entry["episode"]]["state"] = "verified"
    note(series, f"{label} · media checked")
    return StepResult.done(next_step="transcode")
