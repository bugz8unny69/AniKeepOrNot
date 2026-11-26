"""Shared step types and results for the series job."""
from dataclasses import dataclass

STEP_SEQUENCE = (
    "get_metadata",
    "check_release",
    "reserve_episodes",
    "download",
    "check_media",
    "transcode",
    "stage",
)


@dataclass(frozen=True)
class StepResult:
    action: str  # "done", "wait", "drop"
    next_step: str | None = None
    wake_at_ms: int | None = None
    why: str | None = None

    @classmethod
    def done(cls, next_step: str | None = None):
        return cls(action="done", next_step=next_step)

    @classmethod
    def wait(cls, until_ms: int | None = None, why: str | None = None):
        return cls(action="wait", wake_at_ms=until_ms, why=why)

    @classmethod
    def drop(cls, why: str):
        return cls(action="drop", why=why)


def next_step_name(current_step: str) -> str:
    """Return the name of the next step following current_step, or 'done' if at the end."""
    try:
        idx = STEP_SEQUENCE.index(current_step)
        if idx + 1 < len(STEP_SEQUENCE):
            return STEP_SEQUENCE[idx + 1]
    except ValueError:
        pass
    return "done"


SERVICE_WAIT_MS = 60_000           # qBittorrent down: try again in a minute.
SETUP_WAIT_MS = 10 * 60_000        # Not set up (address, login, folder): the series sleeps; Retry wakes it.


async def with_client(context, torrent, work):
    """Run work(client) in a thread with a logged-in client for this torrent.

    Returns work's StepResult. qBittorrent being down or not set up is a wait
    (the series sleeps, the torrent keeps its place); an answer that doesn't fit
    (ClientInvalid: not our torrent, a bad response) drops the torrent with why.
    Anything else is a program error and passes through.
    """
    from src.anime.acquisition.settings import DownloadConfigurationError
    from src.anime.acquisition.submission import (ClientAuthenticationFailed, ClientInvalid,
                                                  ClientUnavailable, ClientUnsupported)
    from src.anime.async_io import blocking
    from src.anime.configuration_schema import ConfigurationError
    from src.anime.series_job.torrents import marker
    from src.anime.secrets import SecretStorageError
    from src.anime.acquisition.metadata import MetadataInvalid

    def run():
        return work(context.torrents.client(context.configuration, marker(torrent)))
    try:
        return await blocking(run)
    except ClientUnavailable:
        return StepResult.wait(context.now + SERVICE_WAIT_MS, why="SERVICE_UNAVAILABLE")
    except (ClientAuthenticationFailed, ClientUnsupported, DownloadConfigurationError, ConfigurationError,
            SecretStorageError):
        return StepResult.wait(context.now + SETUP_WAIT_MS, why="SERVICE_SETUP_INVALID")
    except ClientInvalid as exc:
        return StepResult.drop(f"SERVICE_RESPONSE_INVALID: {exc}"[:200])
    except MetadataInvalid:
        return StepResult.drop("ACQUISITION_METADATA_INVALID")
