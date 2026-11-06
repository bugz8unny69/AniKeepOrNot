"""Hash-bound magnet bootstrap (backfill-selected or ordinary webhook) using the negotiated qBittorrent adapter in the existing PREPARE job.

Never produces a BEP3 Manifest or a torrent send permit for SUBMIT_TORRENT.
Observed inventories enter the existing transfer pipeline without another add.
"""
import logging

from src.nextgen.async_io import blocking
from src.nextgen.diagnostics import exception
from src.nextgen.acquisition.contracts import AcquisitionProposal
from src.nextgen.acquisition.qbittorrent import Client as QBClient
from src.nextgen.acquisition.settings import (download_options, size_limits, DownloadConfigurationError,
    SizeLimitExceeded, SizeEvidenceMissing)
from src.nextgen.acquisition.metadata import (ObservedInventory, MetadataInvalid,
    AcquisitionConflict, SourceMissing)
from src.nextgen.acquisition.metadata_service import (
    verify_torrent_ownership, poll_or_stop_metadata, shared_limiter,
)
from src.nextgen.acquisition.submission import (
    ClientInvalid, ClientUnavailable, ClientAuthenticationFailed, ClientUnsupported,
)
from src.nextgen.job_contracts import Outcome, failure_facts
from src.nextgen.storage.magnet_intake import MagnetJournal
from src.nextgen.storage.release_selection import acquisition_identity, SelectionConflict
from src.nextgen.storage.acquisition import source_magnet
from src.nextgen.services.base import service_facts

PENDING=Outcome('defer','TORRENT_METADATA_WAITING',delay_ms=60000)


def make_bootstrap(secrets,*,client_factory=QBClient,journal_factory=MagnetJournal):
    """Only trusted worker composition supplies this callback; no webhook input."""
    # Limit simultaneous metadata discovery in this worker.
    metadata_slots=shared_limiter().semaphore

    async def bootstrap(source,context):
        configuration=context.configuration
        try:
            database=configuration.value('storage.database_path')
            from src.nextgen.storage.manual_release import metainfo_for
            uploaded=await blocking(metainfo_for,database,source.event_id,source.info_hash)
            if uploaded is not None:
                # A supplied .torrent has its own original info bytes. Let
                # PREPARE validate them, and SUBMIT add that exact file.
                return None
            identity=await blocking(acquisition_identity,database,source)
            magnet=None
            if identity is None:
                # Ordinary webhooks resolve the same sanitized hash-bound
                # magnet as backfill; there is no separate HTTP source setting.
                magnet=await blocking(source_magnet,database,source)
            options=download_options(configuration)
            credentials=await blocking(secrets.qbittorrent_credentials)
            probe=client_factory(options,credentials)
            await blocking(probe.login)
            if context.cancelled.is_set():return PENDING
            journal=journal_factory(configuration.value('storage.database_path'))
            # Always recover an earlier admitted magnet, even after the daemon
            # upgrades to an API with metadata-only fetch support.
            intent=await blocking(journal.existing,context.claim,source,
                                  options,probe.major)
            if intent is None and probe.metadata_strategy=='fetch':
                return None
            permit=None
            if intent is None:
                if identity is not None:
                    # A backfill delivery carries its own sanitized magnet.
                    magnet=await blocking(source_magnet,database,source)
                before=await blocking(probe.inspect,source.info_hash)
                if context.cancelled.is_set():return PENDING
                intent,permit,_=await blocking(
                    journal.authorize,context.claim,source,options,probe.major,
                    before is not None)
            if intent.state in ('REVIEW','EXTERNAL'):
                return Outcome('review','TORRENT_ALREADY_PRESENT' if intent.state=='EXTERNAL'
                               else 'TORRENT_OWNERSHIP_CONFLICT')
            owned=client_factory(intent.options,credentials)
            await blocking(owned.login)
            if permit is not None:
                # Once admitted, a failed or interrupted send is UNCERTAIN.
                # The journal never gives a second permit on replay.
                if context.cancelled.is_set():return PENDING
                await blocking(journal.guard,context.claim,intent)
                try:
                    await blocking(owned.add_legacy_magnet,
                                   magnet,intent,permit)
                except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                    return PENDING
            current=await blocking(owned.inspect,source.info_hash)
            if current is None:
                if await blocking(journal.metadata_expired,context.claim,intent):
                    return Outcome('fail','TORRENT_METADATA_TIMEOUT')
                return PENDING
            # Do not stop/restart any torrent without the exact journal tag.
            if not verify_torrent_ownership(current, intent.marker,
                                            intent.options.download_root,
                                            intent.options.category):
                state=await blocking(journal.reconcile,context.claim,intent,current)
                if state=='REVIEW':
                    return Outcome('review','TORRENT_OWNERSHIP_CONFLICT')
                return PENDING
            snapshot = await poll_or_stop_metadata(
                owned, intent, current, context.cancelled, configuration,
                semaphore=metadata_slots)
            if snapshot is None and context.cancelled.is_set():
                return PENDING
            # Only the double-checked stopped snapshot can transition to
            # OWNED_STOPPED. A single opportunistic files read cannot.
            state=await blocking(journal.reconcile,context.claim,intent,snapshot)
            if state in ('REVIEW','EXTERNAL'):
                return Outcome('review','TORRENT_ALREADY_PRESENT' if state=='EXTERNAL'
                               else 'TORRENT_OWNERSHIP_CONFLICT')
            if state=='OWNED_STOPPED' and snapshot is not None:
                inventory=ObservedInventory.from_observation(snapshot)
                size_limits(configuration).admit(
                    inventory.files[inventory.selected_index].size)
                await blocking(journal.capture,context.claim,intent,source,inventory)
                return Outcome(value=AcquisitionProposal(source,inventory,
                    configuration.desired_revision,size_limits(configuration),context.claim.job_id))
            if await blocking(journal.metadata_expired,context.claim,intent):
                return Outcome('fail','TORRENT_METADATA_TIMEOUT')
            return PENDING
        except ClientUnavailable:
            return Outcome('retry','SERVICE_UNAVAILABLE',facts=service_facts('qbittorrent'))
        except (SizeLimitExceeded, MetadataInvalid) as exc:
            exception(context.logger,'OPERATION_FAILED',exc,level=logging.WARNING,
                      reason_code=exc.reason_code,component='worker',
                      job_id=context.claim.job_id,event_id=context.claim.event_id,
                      attempt=context.claim.attempt)
            return Outcome('fail',exc.reason_code,facts=getattr(exc,'facts',None) or failure_facts(exc))
        except (AcquisitionConflict,
                SizeEvidenceMissing,SourceMissing) as exc:
            # Same safe traceback (line numbers only) to locate the failed check.
            exception(context.logger,'OPERATION_FAILED',exc,level=logging.WARNING,
                      reason_code=exc.reason_code,component='worker',
                      job_id=context.claim.job_id,event_id=context.claim.event_id,
                      attempt=context.claim.attempt)
            return Outcome('review',exc.reason_code,facts=getattr(exc,'facts',None))
        except (ClientInvalid,ClientAuthenticationFailed,ClientUnsupported,
                DownloadConfigurationError) as exc:
            # Name the failed check (as SUBMIT_TORRENT does), not a generic conflict.
            # The safe traceback (line numbers only) shows which qBittorrent call failed.
            exception(context.logger,'OPERATION_FAILED',exc,level=logging.WARNING,
                      reason_code=exc.reason_code,component='worker',
                      job_id=context.claim.job_id,event_id=context.claim.event_id,
                      attempt=context.claim.attempt)
            return Outcome('review',exc.reason_code)
        except SelectionConflict:
            return Outcome('review','TORRENT_OWNERSHIP_CONFLICT')
    return bootstrap
