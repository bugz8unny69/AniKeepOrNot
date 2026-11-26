"""Stage each checked file for you to watch: copied into the staging folder with its NFOs.

staging.specification lays out the files (Show [tvdbid-..] [anidbid-..]/Season NN/
Show - SNNENN.mkv, episode and series NFOs); staging.Publisher copies them with
temporary names and renames them, its records kept in the torrent's entry
(series_job/staging.py). The plan, with its temporary names, is saved to the row
before anything is copied, so an interrupted copy resumes with the same names.
The torrent keeps its files (COPY): qBittorrent can keep seeding.
"""
from types import SimpleNamespace

from src.anime.async_io import blocking
from src.anime.series_job.staging import SeriesStagingJournal
from src.anime.series_job.steps.common import SERVICE_WAIT_MS, SETUP_WAIT_MS, StepResult
from src.anime.series_job.store import SeriesRow, note
from src.anime.series_job.torrents import options_for
from src.anime.staging import (Publisher, StagingBusy, StagingConfiguration, StagingReview, StagingUnavailable,
                               disjoint, root_identity, specification)


def plan_for(configuration, series, torrent, entry):
    root = configuration.value("storage.user_evaluation_root")
    source_root = options_for(configuration, torrent["qb"]["marker"]).download_root
    disjoint(root, (source_root, configuration.value("storage.download_root"),
                    configuration.value("storage.keep_root"), configuration.value("storage.database_path")))
    season, number = int(entry["episode"][1:3]), int(entry["episode"][4:])
    identity = (series.anidb_id, entry["anidb_episode_id"], torrent["identity"]["anime"]["tvdb_series_id"],
                season, number, entry.get("tvdb_episode_id"), series.title)
    evidence = {"path": entry["path"], "sha256": entry["media"]["sha256"], "file": entry["media"]["file"]}
    if entry.get("transcoded"):
        # The transcode output in <staging>/.transcode is moved into place; the torrent's copy stays.
        source_root, evidence = root, entry["transcoded"]
    plan = specification(SimpleNamespace(options=SimpleNamespace(download_root=source_root)), evidence, identity,
                         root, configuration.desired_revision)
    plan["source_root"] = source_root
    plan["transfer"] = "MOVE" if source_root == root else "COPY"
    plan["root_identity"] = root_identity(root)
    return plan


async def stage(series: SeriesRow, torrent: dict, context) -> StepResult:
    configuration = context.configuration
    label = (f"series {series.anidb_id} {series.title} · {', '.join(torrent['episodes'])} "
             f"{torrent.get('release_name', '')}")
    plans = torrent.setdefault("staging", {}).setdefault("plans", {})
    try:
        if any(entry["episode"] not in plans for entry in torrent["files"]):
            for entry in torrent["files"]:
                plans.setdefault(entry["episode"], plan_for(configuration, series, torrent, entry))
            context.checkpoint()                  # Temporary names are saved before any file exists.
        journal = SeriesStagingJournal(configuration.value("storage.database_path"), context.claim,
                                       series.torrents, torrent)
        publisher = Publisher(journal, context.claim, context.cancelled)
        for entry in torrent["files"]:
            if series.episodes[entry["episode"]]["state"] == "staged":
                continue
            plan = plans[entry["episode"]]
            receipts = await blocking(publisher.publish, plan)
            media = next(e["path"] for e in plan["entries"] if e["role"] == "media")
            entry["staged_file"] = next(r["file"] for r in receipts if r["role"] == "media")   # Keep checks it.
            series.episodes[entry["episode"]] = {"state": "staged", "torrent": torrent["infohash"], "file": media}
            note(series, f"{label} · {entry['episode']} staged: {media}")
    except StagingConfiguration as exc:
        return StepResult.wait(context.now + SETUP_WAIT_MS, why=exc.reason_code)
    except (StagingUnavailable, StagingBusy) as exc:
        return StepResult.wait(context.now + SERVICE_WAIT_MS, why=exc.reason_code)
    except StagingReview as exc:
        note(series, f"{label} · dropped: staging found a file it did not expect ({exc})")
        return StepResult.drop(exc.reason_code)
    return StepResult.done(next_step="done")
