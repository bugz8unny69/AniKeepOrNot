"""qBittorrent-native torrent metadata access.

No separately configured HTTP source is used. A legacy URL validator remains
only to verify v7-v11 settings *before* dropping them during v12 migration.
"""
import asyncio
import re
import urllib.parse

from src.nextgen.async_io import blocking
from src.nextgen.acquisition.metadata import infohash, MetadataInvalid
from src.nextgen.acquisition.settings import (
    endpoint, download_options, DownloadConfigurationError,
)
from src.nextgen.acquisition.submission import (
    ClientAuthenticationFailed, ClientInvalid, ClientUnavailable, ClientUnsupported,
)
from src.nextgen.acquisition.qbittorrent import Client as QbClient


class MetadataSourceConfigurationError(ValueError):
    reason_code = 'TORRENT_METADATA_CONFIGURATION_INVALID'
    def __init__(self):
        super().__init__('Configure qBittorrent credentials and download folder.')


class MetadataSourceUnavailable(RuntimeError):
    reason_code = 'TORRENT_METADATA_UNAVAILABLE'
    def __init__(self):
        super().__init__('qBittorrent could not provide torrent metadata.')


class MetadataSourceDenied(ValueError):
    reason_code = 'TORRENT_METADATA_DENIED'
    def __init__(self):
        super().__init__('qBittorrent rejected the metadata request.')


class MetadataClientUnsupported(MetadataSourceDenied):
    reason_code = 'TORRENT_METADATA_CLIENT_UNSUPPORTED'
    def __init__(self):
        ValueError.__init__(self, 'The batch requires a safely owned stopped torrent before older qBittorrent can export metadata.')


class MetadataSourceWaiting(RuntimeError):
    reason_code = 'TORRENT_METADATA_WAITING'
    def __init__(self, delay_ms=60000):
        self.delay_ms = delay_ms
        super().__init__('qBittorrent is still retrieving torrent metadata.')


def source_url(value):
    """Historical v7-v11 URL validation; never used to fetch current metadata."""
    try:
        if type(value) is not str or not value or len(value)>2048:raise ValueError()
        parsed=urllib.parse.urlsplit(value)
        authority=endpoint(urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,'','','')))
        if (parsed.query or parsed.fragment or '?' in value or '#' in value or '%' in value
                or '\\' in value or not value.isascii() or any(ord(c)<=32 or ord(c)==127 for c in value)):
            raise ValueError()
        path=parsed.path.rstrip('/')
        if path and (not path.startswith('/')
                     or any(not re.fullmatch('[A-Za-z0-9_-]+',p)
                            for p in path[1:].split('/'))):
            raise ValueError()
        return authority+path
    except (ValueError, TypeError, AttributeError):
        raise MetadataSourceConfigurationError() from None


def _event_magnet(database,event_id,digest):
    """Use only the original event's hash-bound, sanitized magnet trackers."""
    from src.nextgen.storage.migrations import _open
    from src.nextgen.job_contracts import parse_payload
    from src.nextgen.acquisition.metadata import hash_magnet
    db=_open(database,readonly=True,timeout_ms=5000)
    try:
        row=db.execute('SELECT raw_body FROM incoming_events WHERE event_id=?',
                       (event_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise MetadataSourceDenied()
    try:
        declared=parse_payload(bytes(row[0]).decode('utf-8')).get('magnet_uri')
        magnet=hash_magnet(digest,declared)
    except (ValueError,TypeError,UnicodeError,AttributeError):
        magnet=None
    if magnet is None:
        raise MetadataSourceDenied()
    return magnet


async def _qb_metadata(source, digest, configuration, cancelled, *,
                       secrets, client_factory, batch, claim=None, event_id=None):
    """Version-gated metadata access; legacy batch sends require a durable lease.

    Web API fetch returns BEP3 bytes without adding a torrent. Older clients
    use one journaled paused/stopped magnet, which later batch jobs reuse.
    """
    if cancelled.is_set():
        raise asyncio.CancelledError()
    if secrets is None:
        raise MetadataSourceConfigurationError()
    try:
        options=download_options(configuration)
        credentials=await blocking(secrets.qbittorrent_credentials)
        qb=client_factory(options,credentials)
        await blocking(qb.login)
        if cancelled.is_set():
            raise asyncio.CancelledError()
        if qb.metadata_strategy=='fetch':
            result=await blocking(qb.fetch_metadata,source,digest,batch=batch)
            if result is None:
                raise MetadataSourceWaiting()
            return result[1]

        if not batch:
            # PREPARE_ACQUISITION handles old single torrents through its
            # existing fenced magnet bootstrap, not this batch admission.
            raise MetadataClientUnsupported()

        from src.nextgen.storage.batch_metadata_admission import (
            BatchMetadataJournal, lookup, _inventory,
        )
        from src.nextgen.batch.qb_client import BatchClient
        from src.nextgen.acquisition.metadata import hash_magnet, ObservedBatchInventory
        from src.nextgen.storage.batch_metadata_admission import STOPPED

        database=configuration.value('storage.database_path')
        owned,stored=await blocking(lookup,database,digest)
        if owned is None:
            if claim is None or event_id is None:
                raise MetadataClientUnsupported()
            journal=BatchMetadataJournal(database)
            # Never adopt an existing matching hash, even if it carries
            # another application's "ad-" tag.
            before=await blocking(qb.inspect,digest)
            owned,permit=await blocking(
                journal.authorize,claim,event_id,digest,options,qb.major,
                before is not None)
            if owned.state in ('EXTERNAL','REVIEW'):
                raise MetadataSourceDenied()
            target=BatchClient(owned.options,credentials)
            await blocking(target.login)
            if permit is not None:
                await blocking(journal.guard,claim,owned)
                try:
                    magnet=await blocking(_event_magnet,database,event_id,digest)
                    await blocking(target.add_early_batch_magnet,
                                   magnet,owned,permit)
                except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                    # The network send is uncertain; never issue it again.
                    raise MetadataSourceWaiting() from None
            current=await blocking(target.inspect,digest)
            if current is None:
                raise MetadataSourceWaiting()
            if (current.save_path.rstrip('/')!=owned.options.download_root
                    or current.category!=owned.options.category
                    or current.auto_tmm is not False
                    or 'ad-'+owned.marker not in current.tags
                    or any(t.startswith('ad-') and t!='ad-'+owned.marker
                           for t in current.tags)):
                await blocking(journal.observe,claim,owned,current)
                raise MetadataSourceDenied()
            if current.state not in STOPPED:
                observed=await blocking(target.stop_legacy_metadata,owned)
            elif current.files:
                observed=await blocking(target.inspect_legacy_metadata,
                    digest,owned.marker,owned.options.download_root)
            else:
                observed=await blocking(target.poll_legacy_metadata,
                                         owned,cancelled,
                                         attempts=configuration.value('qbittorrent.metadata_poll_attempts'),
                                         interval=configuration.value('qbittorrent.metadata_poll_seconds'))
            updated=await blocking(journal.observe,claim,owned,observed)
            if updated.state in ('REVIEW','EXTERNAL'):
                raise MetadataSourceDenied()
            if updated.state!='OWNED_STOPPED':
                raise MetadataSourceWaiting()
            owned,stored=await blocking(lookup,database,digest)

        # Never trust just the marker: recheck the complete journal-owned
        # snapshot before returning metadata to any downstream batch stage.
        target=BatchClient(owned.options,credentials)
        await blocking(target.login)
        # After SUBMIT_BATCH adopts the exact journal-owned magnet, transfer
        # jobs may legitimately be downloading. Keep verifying identity and
        # immutable files without falsely requiring a stopped torrent.
        observed=await blocking(target.inspect_owned_batch_metadata,
                                digest,owned.marker,owned.options.download_root,
                                require_stopped=owned.state!='ADOPTED')
        if observed is None or stored is None or observed.files!=stored.files:
            raise MetadataSourceDenied()
        if cancelled.is_set():
            raise asyncio.CancelledError()
        if target.metadata_strategy=='guarded_export':
            _,raw=await blocking(target.export_metainfo,digest,batch=True)
            # The qB observation and the BEP3 parser must describe the same
            # inventory before the pipeline switches to cryptographic proof.
            from src.nextgen.acquisition.metadata import parse_batch_metainfo
            verified=parse_batch_metainfo(raw,digest)
            if verified.files!=stored.files:
                raise MetadataSourceDenied()
            return raw
        return observed
    except DownloadConfigurationError:
        raise MetadataSourceConfigurationError() from None
    except ClientUnsupported:
        raise MetadataClientUnsupported() from None
    except ClientUnavailable:
        raise MetadataSourceUnavailable() from None
    except (ClientAuthenticationFailed,ClientInvalid):
        raise MetadataSourceDenied() from None
    except Exception as exc:
        from src.nextgen.acquisition.metadata import AcquisitionConflict
        if isinstance(exc,AcquisitionConflict):
            raise MetadataSourceDenied() from None
        raise

def make_reader(*, client_factory=QbClient, secrets=None):
    """Single-episode metadata reader. Older clients use PREPARE bootstrap."""
    async def read_metainfo(source, configuration, cancelled):
        if cancelled.is_set():
            raise asyncio.CancelledError()
        from src.nextgen.storage.manual_release import metainfo_for
        uploaded = await blocking(
            metainfo_for, configuration.value('storage.database_path'),
            source.event_id, infohash(source.info_hash))
        if uploaded is not None:
            # Exact uploaded BEP3 bytes flow through the existing strict
            # PREPARE -> SUBMIT_TORRENT pipeline, not a metadata-only magnet.
            return uploaded
        from src.nextgen.storage.acquisition import source_magnet
        magnet = await blocking(
            source_magnet,configuration.value('storage.database_path'),source)
        return await _qb_metadata(magnet, infohash(source.info_hash), configuration,
                                  cancelled,secrets=secrets,
                                  client_factory=client_factory,batch=False)
    return read_metainfo


def make_batch_reader(*, client_factory=QbClient, secrets=None):
    """Only a normalized hash is supplied to the batch verifier."""
    async def read_batch_metainfo(expected_hash, configuration, cancelled, *, claim=None, event_id=None):
        digest = infohash(expected_hash)
        return await _qb_metadata(digest,digest,configuration,cancelled,
                                  secrets=secrets,client_factory=client_factory,
                                  batch=True,claim=claim,event_id=event_id)
    return read_batch_metainfo
