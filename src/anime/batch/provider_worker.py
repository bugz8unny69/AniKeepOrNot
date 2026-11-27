"""Trusted batch-release provider composition, separate from single episodes.

This verifies immutable metadata and AniDB/Anime-Lists/TVDB episode facts;
it never submits a torrent or makes a trial/Keep decision.
"""
from dataclasses import dataclass, field
import inspect

from src.anime.acquisition.metadata import BatchInventory, ObservedBatchInventory, infohash, read_batch_inventory
from src.anime.batch.recognition import release_scope_hint
from src.anime.batch.verification import VerifiedBatch, verify_batch_files
from src.anime.providers.errors import ProviderAuthenticationFailed
from src.anime.providers.http import Clients, Credentials, Options, json_object
from src.anime.providers.identity import ClientPool, delivery_title
from src.anime.release_contracts import ReleaseInput, ReleaseNotReady
from src.anime.async_io import blocking


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

        # Reject known incompatible audio releases before metadata or probe download (WI-06)
        from src.anime.acquisition.audio import is_audio_incompatible
        if is_audio_incompatible(delivery_title(source), configuration.value('acquisition.audio_check')):
            from src.anime.acquisition.media import AudioMissing
            raise AudioMissing(('eng', 'jpn'), (), ('eng', 'jpn'))
        # The series job's reader: the .torrent bytes its metadata step saved.
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

        # Validate per-episode file size limits before downloading video probe (WI-07)
        from src.anime.acquisition.settings import size_limits
        limits = size_limits(configuration)
        by_index = {f.index: f for f in inventory.files}
        for fact in verified.files:
            file = by_index.get(fact.index)
            if file is not None:
                limits.admit(file.size)

        if cancelled.is_set():
            raise ReleaseNotReady()
        return BatchVerificationCompletion(source,inventory,verified)
    return call
