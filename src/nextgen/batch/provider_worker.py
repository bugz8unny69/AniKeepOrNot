"""Trusted batch-release provider composition, separate from single episodes.

This verifies immutable metadata and AniDB/Anime-Lists/TVDB episode facts;
it never submits a torrent or makes a trial/Keep decision.
"""
from dataclasses import dataclass, field
import inspect

from src.nextgen.acquisition.metadata import BatchInventory, ObservedBatchInventory, infohash, read_batch_inventory
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.batch.verification import VerifiedBatch, verify_batch_files
from src.nextgen.providers.errors import ProviderAuthenticationFailed
from src.nextgen.providers.http import Clients, Credentials, Options, json_object
from src.nextgen.providers.identity import ClientPool, delivery_title
from src.nextgen.release_contracts import ReleaseInput, ReleaseNotReady
from src.nextgen.worker import blocking


@dataclass(frozen=True)
class BatchCandidatePooled:
    """This delivery waits in its scope's candidate pool instead of proceeding.

    winner_candidate_id: a higher-ranked READY candidate that nothing is
    running; the job's completion activates and schedules it (WI-11).
    """
    source: ReleaseInput = field(repr=False)
    candidate_id: int
    scope_id: int
    info_hash: str
    winner_candidate_id: int | None = None


async def _observe(observe_existing,configuration,digests):
    """Read-only qBittorrent observations, mapped to the reader's typed errors.

    qBittorrent problems are not ignored: the job waits or stops exactly as
    the metadata reader would for the same failure.
    """
    from src.nextgen.acquisition.metainfo_source import (
        MetadataClientUnsupported, MetadataSourceConfigurationError, MetadataSourceDenied,
        MetadataSourceUnavailable,
    )
    from src.nextgen.acquisition.settings import DownloadConfigurationError
    from src.nextgen.acquisition.submission import (
        ClientAuthenticationFailed, ClientInvalid, ClientUnavailable, ClientUnsupported,
    )
    try:
        return await blocking(observe_existing,configuration,digests)
    except DownloadConfigurationError:
        raise MetadataSourceConfigurationError() from None
    except ClientUnsupported:
        raise MetadataClientUnsupported() from None
    except ClientUnavailable:
        raise MetadataSourceUnavailable() from None
    except (ClientAuthenticationFailed,ClientInvalid):
        raise MetadataSourceDenied() from None


@dataclass(frozen=True)
class BatchVerificationCompletion:
    source: ReleaseInput = field(repr=False)
    inventory: BatchInventory = field(repr=False)
    verified: VerifiedBatch

    def __post_init__(self):
        if type(self.source) is not ReleaseInput or type(self.inventory) not in (BatchInventory,ObservedBatchInventory):
            raise ValueError('Typed release and batch metadata required')
        if type(self.verified) is not VerifiedBatch:
            raise ValueError('Batch provider proof required')
        if (self.source.anidb_id,self.source.tvdb_series_id) != (
                self.verified.anidb_id,self.verified.tvdb_series_id):
            raise ValueError('Provider proof belongs to a different series')
        if (self.inventory.info_hash,self.inventory.info_sha256) != (
                self.verified.info_hash,self.verified.info_sha256):
            raise ValueError('Provider proof belongs to a different torrent')


def make_batch_verifier(options, credentials, read_batch_metainfo, *,
                        client_pool=None, client_factory=Clients, observe_existing=None):
    """Return async batch verifier callable suitable for the release job.

    The configured metainfo reader accepts ONLY a 40-character infohash,
    never an untrusted URL. Client factory and credentials are service-owned.
    """
    if ((type(options) is not Options and not callable(options))
            or not callable(credentials)
            or not inspect.iscoroutinefunction(read_batch_metainfo)):
        raise ValueError('Incomplete trusted batch provider composition')
    pool=client_pool or ClientPool(client_factory)
    if observe_existing is not None and not callable(observe_existing):
        raise ValueError('Existing-metadata observer must be callable')

    async def _coordinate(source,digest,document,season,configuration,claim):
        """Series-scoped controller step (WI-01/WI-02).

        Network reads (qBittorrent file lists) happen first, outside any
        transaction; the decision is then made and committed in one short
        write transaction that re-reads the controller state.
        """
        from src.nextgen.storage.batch_candidates import coordinate_release, scope_releases
        from src.nextgen.storage.job_queue import JobQueue
        from src.nextgen.storage.sqlite import transaction
        queue=JobQueue(configuration.value('storage.database_path'))

        def releases():
            with queue.connection() as db:
                return scope_releases(db,source.anidb_id,season)
        observations={}
        if observe_existing is not None:
            digests=(digest,*(r.info_hash for r in await blocking(releases)))
            observations=await _observe(observe_existing,configuration,digests)

        def decide():
            with queue.connection() as db,transaction(db):
                return coordinate_release(
                    db,anidb_id=source.anidb_id,tvdb_series_id=source.tvdb_series_id,season=season,
                    info_hash=digest,title=delivery_title(source),event_id=source.event_id,
                    job_id=claim.job_id,seeders=document.get('seeders'),size_bytes=document.get('size'),
                    observations=observations,audio_check=configuration.value('acquisition.audio_check'))
        return await blocking(decide)

    async def call(source,configuration,cancelled,*,claim=None):
        if type(source) is not ReleaseInput:
            raise ValueError('Release source required')
        if cancelled.is_set():
            raise ReleaseNotReady()
        hint=release_scope_hint(delivery_title(source))
        if hint.kind not in ('season_pack','mixed_pack') or not hint.season_hint:
            raise ReleaseNotReady('RELEASE_TYPE_UNSUPPORTED')
        document=json_object(source.raw_body)
        try:
            digest=infohash(document.get('torrent_infohash'))
        except ValueError:
            raise ReleaseNotReady('RELEASE_UNRESOLVED') from None

        if claim is not None:
            decision=await _coordinate(source,digest,document,hint.season_hint,configuration,claim)
            if decision.action=='POOLED':
                return BatchCandidatePooled(source,decision.candidate_id,decision.scope_id,digest,
                                            decision.winner_candidate_id)
        # The built-in reader needs the leased VERIFY_RELEASE claim before
        # issuing any older-client stopped-magnet admission. Custom injected
        # readers used by tests retain their three-argument contract.
        if claim is not None and 'claim' in inspect.signature(read_batch_metainfo).parameters:
            raw=await read_batch_metainfo(digest,configuration,cancelled,
                                          claim=claim,event_id=source.event_id)
        else:
            raw=await read_batch_metainfo(digest,configuration,cancelled)
        inventory=await blocking(read_batch_inventory,raw,digest)
        actual=options(configuration) if callable(options) else options
        secret=credentials()
        if type(secret) is not Credentials:
            raise ProviderAuthenticationFailed()
        client=pool.get(configuration.value('storage.database_path'),actual)
        verified=await blocking(
            verify_batch_files,inventory,client=client,credentials=secret,
            anidb_id=source.anidb_id,tvdb_series_id=source.tvdb_series_id,
            season=hint.season_hint,cancelled=cancelled)
        if cancelled.is_set():
            raise ReleaseNotReady()
        return BatchVerificationCompletion(source,inventory,verified)
    return call
