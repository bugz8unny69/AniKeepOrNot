"""Single authorized stopped add; subsequent attempts are read-only reconciliation."""
import inspect

from src.nextgen.acquisition.metadata import AcquisitionConflict, MetadataInvalid, SourceMissing, parse_metainfo
from src.nextgen.acquisition.settings import DownloadConfigurationError, SizeEvidenceMissing, download_options
from src.nextgen.acquisition.submission import (ClientUnavailable, ClientInvalid, ClientAuthenticationFailed, ClientUnsupported)
from src.nextgen.acquisition.qbittorrent import Client
from src.nextgen.diagnostics import log_history
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration, Successor, failure_facts
from src.nextgen.providers.errors import ProviderDeferred, ProviderUnavailable
from src.nextgen.acquisition.metainfo_source import (MetadataSourceWaiting, MetadataSourceUnavailable,
    MetadataSourceConfigurationError, MetadataSourceDenied)
from src.nextgen.storage.submissions import SubmissionJournal, target_options, _plan, _intent
from src.nextgen.worker import blocking

KIND='SUBMIT_TORRENT'
DEFINITION=HandlerDefinition(KIND,'Submit stopped torrent and reconcile ownership')


def _persist(db,claim,value,*,discovery_kind=None):
    try:
        plan=_plan(db,integer(value,positive=True),claim.event_id)
        intent=_intent(db,plan)
        if intent is None or intent.state!='OWNED_STOPPED':raise AcquisitionConflict()
        if discovery_kind is not None:
            from src.nextgen.storage.episode_reservations import _facts
            from src.nextgen.storage.sonarr_backfill import schedule_first
            source=_facts(db,plan.evaluation_job_id,claim.event_id)
            return schedule_first(db,claim,source,kind=discovery_kind)
    except (ValueError,TypeError) as exc:
        reason=getattr(exc,'reason_code','TORRENT_PROVENANCE_CONFLICT')
        raise CompletionRejected(Outcome('review',reason)) from None


def registration(read_metainfo,secrets,*,client_factory=Client,journal_factory=SubmissionJournal,transfer_kind=None,
                 discovery_kind=None):
    if transfer_kind is not None and (type(transfer_kind) is not str or not transfer_kind.strip()
                                     or len(transfer_kind)>64 or transfer_kind==KIND):
        raise ValueError('invalid download successor')
    if discovery_kind is not None and (type(discovery_kind) is not str or not discovery_kind.strip()
                                      or len(discovery_kind)>64 or discovery_kind in (KIND,transfer_kind)):
        raise ValueError('invalid backfill successor')
    if not inspect.iscoroutinefunction(read_metainfo):raise ValueError('trusted async metadata reader required')
    async def execute(context):
        payload=context.data()
        try:
            if set(payload)!={'preparation_job_id'} or context.claim.event_id is None:raise ValueError()
            preparation=integer(payload['preparation_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        journal=journal_factory(context.configuration.value('storage.database_path'))
        try:
            plan,prior=await blocking(journal.load,context.claim,preparation)
            if prior is not None and prior.state in ('EXTERNAL','REVIEW'):
                return Outcome('review','TORRENT_ALREADY_PRESENT' if prior.state=='EXTERNAL' else 'TORRENT_PROVENANCE_CONFLICT')
            if prior is not None and prior.state=='OWNED_STOPPED' and await blocking(journal.handed_off,context.claim,prior):
                return Outcome(value=preparation)
            options=prior.options if prior is not None else target_options(download_options(context.configuration),plan)
            credentials=await blocking(secrets.qbittorrent_credentials)
            raw=None
            if prior is None:
                # Re-fetch and verify exact prepared metadata before consuming a send right.
                from src.nextgen.storage.acquisition import load_acquisition_input
                source=await blocking(load_acquisition_input,context.configuration.value('storage.database_path'),plan.evaluation_job_id,context.claim.event_id)
                raw=await read_metainfo(source,context.configuration,context.cancelled)
                if await blocking(parse_metainfo,raw,plan.manifest.info_hash)!=plan.manifest:raise MetadataInvalid()
            client=client_factory(options,credentials)
            await blocking(client.login)
            before=await blocking(client.inspect,plan.manifest.info_hash)
            intent,permit,records=await blocking(journal.authorize,context.claim,plan,options,client.major,
                                               before is not None,context.configuration.desired_revision)
            for record in records:log_history(context.logger,record)
            if intent.state=='EXTERNAL':return Outcome('review','TORRENT_ALREADY_PRESENT')
            if permit is not None:
                if context.cancelled.is_set():return Outcome('defer','TORRENT_SUBMISSION_UNCERTAIN',delay_ms=60000)
                await blocking(journal.guard_send,context.claim,intent)
                try:await blocking(client.add_stopped,raw,intent,permit)
                except (ClientUnavailable,ClientInvalid,ClientAuthenticationFailed):
                    # The request may have reached the server. Never issue a replacement add.
                    return Outcome('defer','TORRENT_SUBMISSION_UNCERTAIN',delay_ms=60000)
            after=await blocking(client.inspect,plan.manifest.info_hash)
            state,reason,records=await blocking(journal.observe,context.claim,intent,after)
            for record in records:log_history(context.logger,record)
            if state=='OWNED_STOPPED':return Outcome(value=preparation)
            if state in ('EXTERNAL','REVIEW'):return Outcome('review',reason)
            return Outcome('defer',reason,delay_ms=60000)
        except ClientUnavailable:return Outcome('retry','DOWNLOAD_CLIENT_UNAVAILABLE')
        except MetadataSourceWaiting as exc:return Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms)
        except MetadataSourceUnavailable as exc:return Outcome('retry',exc.reason_code)
        except (MetadataSourceConfigurationError,MetadataSourceDenied) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
        except ProviderDeferred as exc:return Outcome('defer','JOB_WAITING',delay_ms=exc.delay_ms)
        except ProviderUnavailable as exc:return Outcome('retry',exc.reason_code)
        except (AcquisitionConflict,MetadataInvalid,SourceMissing,SizeEvidenceMissing,DownloadConfigurationError,
                ClientInvalid,ClientAuthenticationFailed,ClientUnsupported) as exc:
            return Outcome('review',exc.reason_code, facts=failure_facts(exc))
    def route(value):
        return () if transfer_kind is None else (Successor.create(transfer_kind,{'preparation_job_id':value}),)
    return Registration(KIND,DEFINITION,execute,persist=lambda db,claim,value:_persist(db,claim,value,discovery_kind=discovery_kind),route=route,
                        successor_kinds=() if transfer_kind is None else (transfer_kind,))
