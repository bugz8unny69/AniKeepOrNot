"""Import one staged episode through Sonarr using verified TVDB placement."""
import time
from src.anime.handlers import HandlerDefinition
from src.anime.job_contracts import Registration, CompletionRejected, Outcome
from src.anime.keep_service import Service
from src.anime.jobs.keep_execution import execute_keep
from src.anime.history import job_attempt, append_job_history

KIND='KEEP_MEDIA'
SERVICES=('sonarr',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Keep staged video in the permanent library')


def _persist(db,claim,value):
    if db.execute('SELECT phase FROM keep_imports WHERE job_id=?',(claim.job_id,)).fetchone()!=('COMPLETE',):
        raise CompletionRejected(Outcome('review','KEEP_IMPORT_REVIEW'))
    from src.anime.post_keep_cleanup import enqueue as enqueue_cleanup
    enqueue_cleanup(db,claim)
    if value and value.get('refresh'):
        from src.anime.jobs.refresh_jellyfin_job import enqueue
        enqueue(db,claim)
    # Always tell Shoko which episode the kept file is (owner, 2026-11-23).
    from src.anime.jobs.link_shoko_job import enqueue as link_shoko
    import json
    data=json.loads(db.execute('SELECT data_json FROM keep_imports WHERE job_id=?',(claim.job_id,)).fetchone()[0])
    link_shoko(db,data['selection']['anidb_id'],claim=claim)
    from src.anime.jobs.keep_execution import library_history
    return library_history(db,claim,value)+(append_job_history(db,job_id=claim.job_id,event_code='KEEP_COMPLETED',reason_code='KEEP_COMPLETE',
        component='worker',occurred_at_ms=time.time_ns()//1000000,details={'attempt':job_attempt(db,claim.job_id)}),)


def registration(secrets=None,*,service_factory=Service):
    async def execute(context):
        return await execute_keep(context, secrets, service_factory=service_factory)
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
