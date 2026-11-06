"""Inspect or release failed episode reservations using the reset worker.

adm recover resv --ep-id <AniDB or TVDB episode ID>           # read-only
adm recover resv --ep-id <AniDB or TVDB episode ID> --release # guarded cleanup + re-search
"""
import argparse
import json
import sys

from src.nextgen.maintenance import MaintenanceGate
from src.nextgen.storage.episode_reset import find_episode, schedule, EpisodeResetUnavailable


def main(argv=None):
    p=argparse.ArgumentParser(prog='adm recover resv',
        description='Inspect verified episode ownership; --release safely queues cleanup and a fresh search.')
    p.add_argument('--config-dir',default='/etc/anime-discovery')
    p.add_argument('--ep-id','--ep_id',dest='ep_id',type=int,required=True,
                   help='Exact AniDB or TVDB episode ID (not SxxEyy or a sequential episode number)')
    p.add_argument('--release',action='store_true',
                   help='Cancel owning operations, wait for cleanup, retire caches and search again')
    p.add_argument('--json',action='store_true')
    args=p.parse_args(argv)
    from src.nextgen.job_recovery import connect
    try:
        database,db=connect(args.config_dir,readonly=True)
        try:
            result=find_episode(db,args.ep_id)
        finally:
            db.close()
        if args.release:
            with MaintenanceGate(database).operation():
                result['reset']=schedule(database,result['anidb_id'],result['anidb_episode'])
        if args.json:
            print(json.dumps(result,indent=2))
        else:
            print('Episode ID:',args.ep_id)
            print('AniDB:',result['anidb_id'],'episode',result['anidb_episode'],
                  '| TVDB: S%02dE%02d' % (result['tvdb_season'],result['tvdb_episode']))
            for reservation_id,state,owner in result['reservations']:
                print('Reservation #%d: %s (owner Job #%d)'%(reservation_id,state,owner))
            print('Manual candidates:',result['manual_candidates'])
            print('Owning operations:',', '.join(str(op) for op,_ in result['owners']) or 'none')
            if args.release:
                print('Reset Job #%s queued; existing reservations are released only after cleanup.'%
                      result['reset']['job_id'])
            else:
                print('Read-only preview. Add --release to schedule safe cleanup and Prowlarr re-search.')
        return 0
    except (ValueError,OSError,RuntimeError) as exc:
        if args.json:
            print(json.dumps({'error':type(exc).__name__,'message':str(exc)}))
        else:
            print('Reservation recovery unavailable:',str(exc) or type(exc).__name__,file=sys.stderr)
        return 1
