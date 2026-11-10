"""Reconcile Sonarr series/import intent and permanent episode-file result."""
from pathlib import PurePosixPath
import re

from src.anime.keep_selection import KeepSelection
from src.anime.acquisition.metadata import safe_path
from src.anime.sonarr import Client, Options, KeepReview, KeepUnavailable, KeepConfiguration, existing_file
from src.anime.storage.keep_imports import (Journal, check_source, SeriesAddBusy, highest_regular_episode,
                                         monitor_mode, take_search)
from src.anime.storage.staging import Journal as StageJournal
from src.anime.checks import here


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
            if cancelled.is_set():raise KeepReview(failed_check=here('KEEP_CANCELLED'))
            journal.fence(claim)
        resolver=self.resolver
        if resolver is None:
            try:
                from src.anime.keep_resolution import make_keep_resolver
                resolver=make_keep_resolver(self.configuration,self.secrets)
            except Exception:
                resolver=None
        phase,data=journal.load(claim,target,self.configuration,resolver=resolver)
        self.completed=None
        if phase=='COMPLETE':
            self.completed=data
            return True
        selection=KeepSelection(**data['selection']);options=Options(**data['options'])
        client=self.client_factory(options,self.secrets.sonarr_api_key())
        version=client.request('system/status')
        if not re.match(r'^4\.',str(version.get('version',''))):raise KeepReview(failed_check=here('VERSION_UNSUPPORTED'))
        series=client.series(selection.tvdb_series_id)
        if phase in ('PLANNED','ADD_UNCERTAIN'):
            # Keep means Sonarr owns the series from now on: add it when missing.
            if series is None:
                if phase=='ADD_UNCERTAIN':
                    # The add was sent and Sonarr still has no series. Never send it again blindly.
                    raise KeepReview(failed_check=here('SERIES_ADD_UNCONFIRMED'))
                series=self._add(client,claim,selection,options,data,guard)
                if series is None:return False
                phase='ADD_UNCERTAIN'
            if type(series.get('id')) is not int or series['id']<=0:raise KeepReview(failed_check=here('SERIES_INVALID_ID'))
            path=PurePosixPath(series['path'])
            if not path.is_relative_to(options.library_root) or path==PurePosixPath(options.library_root):raise KeepReview(failed_check=here('SERIES_PATH_OUTSIDE_LIBRARY'))
            if str(path)!=series['path']:raise KeepReview(failed_check=here('SERIES_PATH_NOT_CANONICAL'))
            safe_path(str(path.relative_to(options.library_root)))
            data['series_id']=series['id'];data['series_path']=str(path)
            journal.change(claim,phase,'SERIES_READY',data);phase='SERIES_READY'
        if series is None or (series.get('id'),series.get('path'))!=(data['series_id'],data['series_path']):raise KeepReview(failed_check=here('SERIES_RECONCILIATION_MISMATCH'))
        episode=client.episode(selection,series)
        if phase=='SERIES_READY':
            if episode.get('episodeFileId',0):raise KeepReview(failed_check=here('EPISODE_ALREADY_HAS_FILE'))
            check_source(data)
            payload=client.import_payload(selection,series,episode)
            data['payload']=payload;data['episode_id']=episode['id']
            guard();journal.change(claim,phase,'IMPORT_UNCERTAIN',data);phase='IMPORT_UNCERTAIN'
            guard();reply=client.request('command',body=payload)
            if type(reply.get('id')) is not int or reply['id']<=0:raise KeepReview(failed_check=here('COMMAND_REJECTED'))
            data['command_id']=reply['id'];journal.change(claim,phase,phase,data)
        if phase!='IMPORT_UNCERTAIN' or episode['id']!=data['episode_id']:raise KeepReview(failed_check=here('PHASE_OR_EPISODE_MISMATCH'))
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
            if len(matches)!=1 or type(matches[0].get('id')) is not int:raise KeepReview(failed_check=here('IMPORT_COMMAND_AMBIGUOUS'))
            data['command_id']=matches[0]['id'];journal.change(claim,phase,phase,data)
        command=client.request('command/'+str(data['command_id']))
        status=str(command.get('status','')).lower()
        if status in ('failed','aborted','cancelled'):raise KeepReview(failed_check=here('COMMAND_FAILED'))
        if status!='completed':return False
        episode=client.episode(selection,series)
        if not episode.get('hasFile') or type(episode.get('episodeFileId')) is not int or episode['episodeFileId']<=0:return False
        record=client.request('episodefile/'+str(episode['episodeFileId']))
        try:proof=existing_file(options,series,episode,record,data['source_file'][2])
        except FileNotFoundError:return False
        data['result']=proof
        guard();journal.change(claim,phase,'COMPLETE',data)
        self.completed=data
        return True

    def _add(self,client,claim,selection,options,data,guard):
        """Add the series to Sonarr once; None when another Keep job is adding it."""
        with self.journal.queue.connection() as db:
            highest=highest_regular_episode(db,selection.anidb_id)
        monitor=monitor_mode(highest,options)
        body=client.add_body(selection.tvdb_series_id,monitor)
        data['series_add']={'monitor':monitor,'highest_episode':highest}
        guard()
        # Recorded before the POST: a lost reply is reconciled by TVDB ID, never resent.
        try:self.journal.change(claim,'PLANNED','ADD_UNCERTAIN',data)
        except SeriesAddBusy:return None
        guard()
        try:added=client.request('series',body=body)
        except KeepConfiguration:raise
        except KeepReview:
            # Sonarr answered and refused, so nothing was added. Retry may add it.
            self.journal.change(claim,'ADD_UNCERTAIN','PLANNED',data)
            raise KeepReview(failed_check=here('SERIES_ADD_REJECTED')) from None
        series=client.series(selection.tvdb_series_id)
        if series is None or series.get('id')!=added.get('id'):raise KeepReview(failed_check=here('SERIES_ADDED_NOT_FOUND'))
        return series

    def request_search(self,claim):
        """After the series' last staged import, ask Sonarr once for missing episodes.

        Returns 'SENT', 'FAILED', or None when another Keep job of the series is
        still importing or the search was already recorded. A failure never
        fails the Keep.
        """
        data=self.completed
        if not data or 'series_id' not in data:return None
        selection=KeepSelection(**data['selection']);options=Options(**data['options'])
        if not take_search(self.journal.queue,claim,selection.anidb_id):return None
        try:
            self.client_factory(options,self.secrets.sonarr_api_key()).search(data['series_id'])
            return 'SENT'
        except (KeepReview,KeepUnavailable,OSError,ValueError):
            return 'FAILED'
