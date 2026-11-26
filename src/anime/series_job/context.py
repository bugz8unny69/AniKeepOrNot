"""What the series steps work with: the real verifier, qBittorrent, media checks, staging.

Built once by the worker (worker.py _run_series_job). Each attempt gets the
current settings, the time, the cancel event and checkpoint (set by
run_series_attempt). Tests build their own with fakes.
"""
from dataclasses import dataclass, field
from functools import partial

from src.anime.series_job.torrents import Torrents, read_metainfo


@dataclass
class SeriesContext:
    verify: object                  # async (ReleaseInput, configuration, cancelled) -> VerifiedRelease | BatchVerificationCompletion
    torrents: object                # Torrents: logged-in client per torrent
    cancelled: object               # threading/asyncio Event: the worker is stopping
    configuration: object = None
    now: int = 0
    checkpoint: object = field(default=lambda: None)
    db: object = None
    claim: object = None            # The series lease (staging writes its records under it)
    secrets: object = None          # For the transcoder's agent token
    transcoder: object = None       # Tests only: a stand-in for services.transcode.transcode


def build(secrets, cancelled):
    from src.anime.acquisition.metadata import SourceMissing
    from src.anime.batch.provider_worker import make_batch_verifier
    from src.anime.providers.configuration import episode_placement_credentials, options_from_configuration
    from src.anime.providers.identity import ClientPool
    from src.anime.providers.releases import make_verifier

    async def read_saved(digest, configuration, cancelled):
        # The metadata step saved the .torrent bytes; the pack verifier reads them here.
        raw = read_metainfo(configuration, digest)
        if raw is None:
            raise SourceMissing()
        return raw

    pool = ClientPool()
    credentials = partial(episode_placement_credentials, secrets)
    verify = make_verifier(options_from_configuration, credentials, client_pool=pool,
                           batch_verifier=make_batch_verifier(options_from_configuration, credentials,
                                                              read_saved, client_pool=pool))
    return SeriesContext(verify=verify, torrents=Torrents(secrets), cancelled=cancelled, secrets=secrets)
