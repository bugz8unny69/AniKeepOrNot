"""Reconcile Sonarr series/import intent and permanent episode-file result."""
from pathlib import PurePosixPath
import re

from src.nextgen.keep_selection import KeepSelection
from src.nextgen.acquisition.metadata import safe_path
from src.nextgen.sonarr import Client, Options, KeepReview, existing_file
from src.nextgen.storage.keep_imports import Journal, check_source
from src.nextgen.storage.staging import Journal as StageJournal


class Service:
    def __init__(self,configuration,secrets,*,client_factory=Client,
                 journal_factory=Journal,resolver=None):
        self.configuration,self.secrets,self.client_factory=configuration,secrets,client_factory
        self.journal=journal_factory(configuration.value('storage.database_path'))
        self.resolver=resolver

    def run(self,claim,target,cancelled):
        # A stale attempt must finish its I/O before a recovered attempt may send.
        with StageJournal(self.journal.queue.database).lock(claim):
            return self._run(claim,target,cancelled)

    def _run(self,claim,target,cancelled):
        journal=self.journal
        def guard():
            if cancelled.is_set():raise KeepReview(failed_check='src/nextgen/keep_service.py:27 guard KEEP_CANCELLED')
            journal.fence(claim)
        resolver=self.resolver
        if resolver is None:
            try:
                from src.nextgen.keep_resolution import make_keep_resolver
                resolver=make_keep_resolver(self.configuration,self.secrets)
            except Exception:
                resolver=None
        phase,data=journal.load(claim,target,self.configuration,resolver=resolver)
        if phase=='COMPLETE':return True
        selection=KeepSelection(**data['selection']);options=Options(**data['options'])
        client=self.client_factory(options,self.secrets.sonarr_api_key())
        version=client.request('system/status')
        if not re.match(r'^4\.',str(version.get('version',''))):raise KeepReview(failed_check='src/nextgen/keep_service.py:41 _run VERSION_UNSUPPORTED')
        series=client.series(selection.tvdb_series_id)
        if phase in ('PLANNED','ADD_UNCERTAIN'):
            # Keep imports into an existing Sonarr series; it never creates one.
            if series is None:raise KeepReview(failed_check='src/nextgen/keep_service.py:45 _run SERIES_MISSING')
            if type(series.get('id')) is not int or series['id']<=0:raise KeepReview(failed_check='src/nextgen/keep_service.py:46 _run SERIES_INVALID_ID')
            path=PurePosixPath(series['path'])
            if not path.is_relative_to(options.library_root) or path==PurePosixPath(options.library_root):raise KeepReview(failed_check='src/nextgen/keep_service.py:48 _run SERIES_PATH_OUTSIDE_LIBRARY')
            if str(path)!=series['path']:raise KeepReview(failed_check='src/nextgen/keep_service.py:49 _run SERIES_PATH_NOT_CANONICAL')
            safe_path(str(path.relative_to(options.library_root)))
            data['series_id']=series['id'];data['series_path']=str(path)
            journal.change(claim,phase,'SERIES_READY',data);phase='SERIES_READY'
        if series is None or (series.get('id'),series.get('path'))!=(data['series_id'],data['series_path']):raise KeepReview(failed_check='src/nextgen/keep_service.py:53 _run SERIES_RECONCILIATION_MISMATCH')
        episode=client.episode(selection,series)
        if phase=='SERIES_READY':
            if episode.get('episodeFileId',0):raise KeepReview(failed_check='src/nextgen/keep_service.py:56 _run EPISODE_ALREADY_HAS_FILE')
            check_source(data)
            payload=client.import_payload(selection,series,episode)
            data['payload']=payload;data['episode_id']=episode['id']
            guard();journal.change(claim,phase,'IMPORT_UNCERTAIN',data);phase='IMPORT_UNCERTAIN'
            guard();reply=client.request('command',body=payload)
            if type(reply.get('id')) is not int or reply['id']<=0:raise KeepReview(failed_check='src/nextgen/keep_service.py:62 _run COMMAND_REJECTED')
            data['command_id']=reply['id'];journal.change(claim,phase,phase,data)
        if phase!='IMPORT_UNCERTAIN' or episode['id']!=data['episode_id']:raise KeepReview(failed_check='src/nextgen/keep_service.py:64 _run PHASE_OR_EPISODE_MISMATCH')
        if 'command_id' not in data:
            commands=client.request('command')
            matches=[]
            for row in commands:
                body=row.get('body',{})
                if (str(body.get('name','')).lower()=='manualimport' and body.get('importMode')=='copy'
                        and len(body.get('files',[]))==1):
                    source=body['files'][0];wanted=data['payload']['files'][0]
                    if all(source.get(k)==wanted[k] for k in ('path','seriesId','episodeIds')):matches.append(row)
            if not matches:return False
            if len(matches)!=1 or type(matches[0].get('id')) is not int:raise KeepReview(failed_check='src/nextgen/keep_service.py:75 _run IMPORT_COMMAND_AMBIGUOUS')
            data['command_id']=matches[0]['id'];journal.change(claim,phase,phase,data)
        command=client.request('command/'+str(data['command_id']))
        status=str(command.get('status','')).lower()
        if status in ('failed','aborted','cancelled'):raise KeepReview(failed_check='src/nextgen/keep_service.py:79 _run COMMAND_FAILED')
        if status!='completed':return False
        episode=client.episode(selection,series)
        if not episode.get('hasFile') or type(episode.get('episodeFileId')) is not int or episode['episodeFileId']<=0:return False
        record=client.request('episodefile/'+str(episode['episodeFileId']))
        try:proof=existing_file(options,series,episode,record,data['source_file'][2])
        except FileNotFoundError:return False
        data['result']=proof
        guard();journal.change(claim,phase,'COMPLETE',data)
        return True
