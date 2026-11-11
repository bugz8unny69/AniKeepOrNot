"""Optional transcode between media verification and staging; no torrent changes."""
from src.anime.async_io import blocking
from src.anime.handlers import HandlerDefinition
from src.anime.history import integer
from src.anime.job_contracts import Outcome, Registration, Successor
from src.anime.services.transcode import TranscodeWaiting, TranscodeFailed
from src.anime.storage.transcode import prepare
from src.anime.staging import StagingReview
from src.transcode_agent.encode import EncodeError

KIND = 'TRANSCODE_MEDIA'
DEFINITION = HandlerDefinition(KIND, 'Transcode verified video before staging')


def registration(secrets, *, staging_kind='STAGE_USER_EVALUATION_MEDIA'):
    async def execute(context):
        try:
            payload = context.data()
            if set(payload) != {'verification_job_id'}: raise ValueError()
            verification = integer(payload['verification_job_id'], positive=True)
        except (ValueError, TypeError): return Outcome('review', 'JOB_PAYLOAD_INVALID')
        try:
            await blocking(prepare, context, verification, secrets)
            return Outcome(value={'verification_job_id': verification})
        except TranscodeWaiting as exc:
            return Outcome('defer', 'TRANSCODE_WAITING', delay_ms=10000, facts={'service_detail': str(exc)[:500]})
        except (TranscodeFailed, EncodeError) as exc:
            return Outcome('review', 'TRANSCODE_FAILED', facts={'service_detail': str(exc)[:500]})
        except StagingReview:
            return Outcome('review', 'STAGING_REVIEW')
    return Registration(KIND, DEFINITION, execute, successor_kinds=(staging_kind,),
                        route=lambda value: (Successor.create(staging_kind, value),))
