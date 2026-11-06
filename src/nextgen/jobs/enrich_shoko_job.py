"""Independent optional metadata result; never changes the Sonarr Keep result."""
import json

from src.nextgen.async_io import blocking
from src.nextgen.handlers import HandlerDefinition
from src.nextgen.history import integer
from src.nextgen.job_contracts import CompletionRejected, Outcome, Registration
from src.nextgen.messages import DEFAULT_CATALOG
from src.nextgen.secrets import SecretStorageError
from src.nextgen.shoko import lookup, ShokoConfiguration, ShokoUnavailable, ShokoReview
from src.nextgen.storage.sqlite import connect, enqueue_job
from src.nextgen.services.base import service_facts

KIND='ENRICH_SHOKO_METADATA'
SERVICES=('shoko',)  # External services this job needs; see services/README.md.
DEFINITION=HandlerDefinition(KIND,'Read optional Shoko AniDB metadata')


def enqueue(db,claim):
    row=db.execute('SELECT phase,data_json FROM keep_imports WHERE job_id=?',(claim.job_id,)).fetchone()
    if not row or row[0]!='COMPLETE':raise ShokoReview()
    data=json.loads(row[1])
    if 'shoko' in data:raise ShokoReview()
    job=enqueue_job(db,kind=KIND,payload={'keep_job_id':claim.job_id},component='worker',event_id=claim.event_id,created_by_job_id=claim.job_id)
    data['shoko']={'job_id':job,'state':'PENDING'}
    _save(db,claim.job_id,data)


def _save(db,keep,data):
    raw=json.dumps(data,sort_keys=True,allow_nan=False)
    if len(raw.encode())>65536:raise ShokoReview()
    db.execute('UPDATE keep_imports SET data_json=? WHERE job_id=?',(raw,keep))


def facts(db,claim,keep):
    row=db.execute("SELECT k.phase,k.data_json,j.state,j.event_id FROM keep_imports k JOIN jobs j USING(job_id) "
        "JOIN management_actions a USING(job_id) WHERE k.job_id=? AND a.action='KEEP'",(keep,)).fetchone()
    origin=db.execute('SELECT created_by_job_id,event_id FROM jobs WHERE job_id=?',(claim.job_id,)).fetchone()
    if not row or row[0]!='COMPLETE' or row[2]!='DONE' or origin!=(keep,row[3]) or row[3]!=claim.event_id:raise ShokoReview()
    data=json.loads(row[1])
    if data.get('shoko',{}).get('job_id')!=claim.job_id:raise ShokoReview()
    return data


def status(db,keep):
    row=db.execute('SELECT data_json FROM keep_imports WHERE job_id=?',(keep,)).fetchone()
    record=None if row is None else json.loads(row[0]).get('shoko')
    if record is None:return None
    row=db.execute('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(record['job_id'],)).fetchone()
    if row is None:raise ShokoReview()
    return {'job_id':record['job_id'],'state':row[0],'status':DEFAULT_CATALOG.job_status(row[0]),
        'metadata':record.get('metadata'),'reason':None if row[1] is None else DEFAULT_CATALOG.reason(row[1]),
        'status_url':f"/api/v1/jobs/{record['job_id']}"}


def _persist(db,claim,value):
    try:
        keep=value['keep_job_id'];data=facts(db,claim,keep)
        selection=data['selection'];metadata=value['metadata']
        if (metadata['anidb_id'],metadata['anidb_episode_id'])!=(selection['anidb_id'],selection['anidb_episode_id']):raise ShokoReview()
        data['shoko']={'job_id':claim.job_id,'state':'COMPLETE','metadata':metadata}
        _save(db,keep,data)
    except (ValueError,TypeError,KeyError):raise CompletionRejected(Outcome('review','METADATA_ENRICHMENT_REVIEW')) from None


def registration(secrets,*,read=lookup):
    async def execute(context):
        try:
            payload=context.data()
            if set(payload)!={'keep_job_id'}:raise ValueError()
            keep=integer(payload['keep_job_id'],positive=True)
        except (ValueError,TypeError):return Outcome('review','JOB_PAYLOAD_INVALID')
        def run():
            db=connect(context.configuration.value('storage.database_path'))
            try:data=facts(db,context.claim,keep)
            finally:db.close()
            if context.cancelled.is_set():raise ShokoReview()
            selected=data['selection']
            return read(context.configuration,secrets,selected['anidb_id'],selected['anidb_episode_id'])
        try:metadata=await blocking(run)
        except ShokoConfiguration:return Outcome('review','SERVICE_SETUP_INVALID',facts=service_facts('shoko','NOT_CONFIGURED'))
        except (ShokoUnavailable,SecretStorageError):return Outcome('retry','SERVICE_UNAVAILABLE',facts=service_facts('shoko'))
        except (ValueError,TypeError,KeyError):return Outcome('review','METADATA_ENRICHMENT_REVIEW')
        return Outcome(value={'keep_job_id':keep,'metadata':metadata})
    return Registration(KIND,DEFINITION,execute,services=SERVICES,persist=_persist)
