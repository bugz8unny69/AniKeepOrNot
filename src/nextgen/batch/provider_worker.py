"""Trusted batch-release provider composition, separate from single episodes.

This verifies immutable metadata and AniDB/Anime-Lists/TVDB episode facts;
it never submits a torrent or makes a trial/Keep decision.
"""
from dataclasses import dataclass, field
import inspect
import time

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
    source: ReleaseInput = field(repr=False)
    candidate_id: int
    scope_id: int
    info_hash: str


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
                        client_pool=None, client_factory=Clients,
                        qb_client_factory=None, secrets=None):
    """Return async batch verifier callable suitable for the release job.

    The configured metainfo reader accepts ONLY a 40-character infohash,
    never an untrusted URL. Client factory and credentials are service-owned.
    """
    if ((type(options) is not Options and not callable(options))
            or not callable(credentials)
            or not inspect.iscoroutinefunction(read_batch_metainfo)):
        raise ValueError('Incomplete trusted batch provider composition')
    pool=client_pool or ClientPool(client_factory)

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

        database=configuration.value('storage.database_path')
        from src.nextgen.storage.batch_candidates import (
            get_or_create_scope, add_candidate, discover_existing_candidates,
            get_candidate_pool, rank_candidates, reorder_candidates,
            get_active_candidate, next_candidate, activate_candidate,
        )
        import sqlite3
        from src.nextgen.storage.sqlite import transaction

        def _coordinate_pool():
            conn = sqlite3.connect(database, timeout=10)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_candidate_scopes'").fetchone()
                if not has_table:
                    return None
                with transaction(conn):
                    scope = get_or_create_scope(conn, source.anidb_id, source.tvdb_series_id, hint.season_hint)
                    # Examine existing qBittorrent file inventories read-only (WI-02, review item 6)
                    qb_client = None
                    if secrets is not None:
                        try:
                            from src.nextgen.acquisition.qbittorrent import Client as QBClient
                            from src.nextgen.acquisition.settings import download_options
                            factory = qb_client_factory or QBClient
                            opts = download_options(configuration)
                            creds = secrets.qbittorrent_credentials()
                            qb_client = factory(opts, creds)
                            if hasattr(qb_client, 'login'):
                                qb_client.login()
                        except Exception:
                            qb_client = None
                    elif qb_client_factory is not None:
                        try:
                            qb_client = qb_client_factory()
                        except Exception:
                            qb_client = None

                    discover_existing_candidates(conn, scope.scope_id, source.anidb_id, hint.season_hint, client=qb_client)
                    current_has_meta = 0
                    current_meta_json = None
                    if qb_client is not None and hasattr(qb_client, 'inspect'):
                        try:
                            obs = qb_client.inspect(digest, include_files=True)
                            if obs is not None and getattr(obs, 'info_hash', None) == digest and getattr(obs, 'files', None):
                                current_has_meta = 1
                                current_meta_json = json.dumps({'files': [{'index': getattr(f, 'index', i), 'name': getattr(f, 'name', None) or getattr(f, 'path', str(f)), 'size': getattr(f, 'size', 0)} for i, f in enumerate(obs.files)]})
                        except Exception:
                            pass
                    cand = add_candidate(conn, scope.scope_id, digest, delivery_title(source),
                                         source_event_id=source.event_id,
                                         has_metadata=current_has_meta,
                                         metadata_json=current_meta_json,
                                         seeders=document.get('seeders'),
                                         size_bytes=document.get('size'))
                    candidates = get_candidate_pool(conn, scope.scope_id)
                    req_audio = 'japanese_english'
                    try:
                        req_audio = configuration.value('acquisition.audio_check')
                    except Exception:
                        pass
                    ranked = rank_candidates(candidates, audio_check=req_audio)
                    reorder_candidates(conn, scope.scope_id, ranked)
                    active = get_active_candidate(conn, scope.scope_id)

                    op_id = None
                    if claim is not None:
                        op_row = conn.execute(
                            "SELECT operation_id FROM job_operation_members WHERE job_id=?",
                            (claim.job_id,)).fetchone()
                        if op_row:
                            op_id = op_row[0]
                        else:
                            now_ms = int(time.time() * 1000)
                            op_id = conn.execute(
                                "INSERT INTO job_operations(created_at_ms, updated_at_ms) VALUES(?,?)",
                                (now_ms, now_ms)).lastrowid
                            conn.execute(
                                "INSERT INTO job_operation_members(job_id, operation_id) VALUES(?,?)",
                                (claim.job_id, op_id))

                    if active is not None and active.info_hash != digest:
                        return BatchCandidatePooled(source, cand.candidate_id, scope.scope_id, digest)

                    top = next_candidate(conn, scope.scope_id)
                    if top is not None and top.info_hash != digest:
                        return BatchCandidatePooled(source, cand.candidate_id, scope.scope_id, digest)

                    if active is None:
                        activate_candidate(conn, scope.scope_id, digest, operation_id=op_id)
                    return None
            finally:
                conn.close()

        pooled = await blocking(_coordinate_pool)
        if pooled is not None:
            return pooled

        # Reject known incompatible audio releases before metadata or probe download (WI-06)
        req_audio = 'japanese_english'
        try:
            req_audio = configuration.value('acquisition.audio_check')
        except Exception:
            pass
        from src.nextgen.acquisition.audio import is_audio_incompatible
        if is_audio_incompatible(delivery_title(source), req_audio):
            from src.nextgen.acquisition.media import AudioMissing
            raise AudioMissing(('eng', 'jpn'), (), ('eng', 'jpn'))
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

        # Validate per-episode file size limits before downloading video probe (WI-07)
        from src.nextgen.acquisition.settings import size_limits
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
