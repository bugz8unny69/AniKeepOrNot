"""Explicit batch-only qB adapter. Not installed in the worker.

Its sole mutation is a stopped add guarded by the journal's one-shot permit.
No file priority updates, start, or cleanup commands are offered here.
"""
import uuid

from src.anime.acquisition.metadata import parse_batch_metainfo
from src.anime.acquisition.metadata_service import STOPPED
from src.anime.torrent_client.qbittorrent import Client
from src.anime.acquisition.submission import ClientInvalid
from src.anime.storage.batch_submission_journal import (
    BatchSubmissionIntent, BatchSendPermit,
)


class BatchClient(Client):
    def add_early_batch_magnet(self, magnet, intent, permit):
        """Only the journal's one-shot preverification send, always stopped.

        The same ad-<marker> ownership tag is recovered by the later
        submission stage; this method never creates an acquisition permit.
        """
        from src.anime.storage.batch_metadata_admission import EarlyBatchIntent, EarlyBatchPermit
        from src.anime.acquisition.prowlarr_search import _magnet_hash
        from src.anime.acquisition.submission import ClientAuthenticationFailed
        if not self._sid or self.protocol is None:
            raise ClientAuthenticationFailed()
        if (type(intent) is not EarlyBatchIntent or type(permit) is not EarlyBatchPermit
                or intent.state!='UNCERTAIN'
                or self.options!=intent.options or self.major!=intent.api_major
                or self.metadata_strategy!='guarded_export'
                or _magnet_hash(magnet)!=intent.info_hash
                or not self.options.download_root.endswith('/ad-'+intent.marker)):
            raise ClientInvalid()
        payload={'urls':magnet,'savepath':self.options.download_root,
                 'category':self.options.category,
                 **self.protocol.label_fields(intent.marker),
                 **self.protocol.add_fields(True)}
        permit.take(intent)
        self._request('torrents/add',form=payload,limit=4096)


    def inspect_owned_batch_metadata(self, expected_hash, marker, download_root, *, require_stopped=True):
        """Double-check a journal-owned, stopped qB file list without mutation.

        The caller must first persist the one-use early batch admission.
        This returns *observed* metadata, never fabricates a BEP3 SHA256,
        and grants no permission to start or adopt the torrent.
        """
        import re
        from src.anime.acquisition.metadata import (
            infohash, ObservedBatchInventory,
        )
        from src.anime.acquisition.submission import ClientInvalid
        digest = infohash(expected_hash)
        if (not self._sid or self.protocol is None
                or type(marker) is not str
                or re.fullmatch('[a-f0-9]{32}', marker) is None
                or download_root!=self.options.download_root
                or not download_root.endswith('/ad-'+marker)):
            raise ClientInvalid()

        def owned(row):
            return (row is not None and row.info_hash==digest
                    and row.save_path.rstrip('/')==download_root
                    and row.category==self.options.category
                    and row.auto_tmm is False
                    and (not require_stopped or row.state in
                         STOPPED)
                    and 'ad-'+marker in row.tags
                    and not any(t.startswith('ad-') and t!='ad-'+marker
                                for t in row.tags)
                    and bool(row.files))

        before=self.inspect(digest)
        if not owned(before):
            return None
        after=self.inspect(digest)
        if (not owned(after) or after.files!=before.files
                or (require_stopped and after.state!=before.state)):
            raise ClientInvalid()
        return ObservedBatchInventory(digest, after.files)

    def add_batch_stopped(self,raw,intent,permit):
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(permit) is not BatchSendPermit
                or self.options!=intent.options
                or self.major!=intent.api_major
                or intent.state!='UNCERTAIN'):
            raise ClientInvalid()
        try:
            inventory=parse_batch_metainfo(raw,intent.inventory.info_hash)
        except ValueError:
            raise ClientInvalid() from None
        if inventory!=intent.inventory:
            raise ClientInvalid()
        # This single method cannot resume, reprioritize or implicitly start.
        # The exact stopped, owned torrent must be reconciled on any failure.
        boundary='ad'+uuid.uuid4().hex
        while boundary.encode() in raw:
            boundary='ad'+uuid.uuid4().hex
        fields={
            'savepath':intent.options.download_root,
            'category':intent.options.category,
            'tags':'ad-'+intent.marker,
            'stopped' if self.major==5 else 'paused':'true',
            'autoTMM':'false','useDownloadPath':'false',
            'skip_checking':'false','contentLayout':'Original',
        }
        chunks=[]
        for key,value in fields.items():
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+
                           '"\r\n\r\n'+value+'\r\n').encode())
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; '
                       'name="torrents"; filename="source.torrent"\r\n'
                       'Content-Type: application/x-bittorrent\r\n\r\n').encode()
                      +raw+b'\r\n')
        chunks.append(('--'+boundary+'--\r\n').encode())
        permit.take(intent)
        self._request(
            'torrents/add',
            body=b''.join(chunks),
            content_type='multipart/form-data; boundary='+boundary,
            limit=4096)

    def apply_probe_selection(self,intent,fingerprint,permit,action):
        """Mutate only E01 probe priorities; one permit per API command."""
        from src.anime.acquisition.transfers import TransferPermit, TransferConflict
        from src.anime.batch.probe_transfer import (
            verify_probe_snapshot, selection_fingerprint,
        )
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(permit) is not TransferPermit
                or self.options!=intent.options
                or action not in ('skip','wanted')):
            raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.inventory.info_hash)
        if snapshot is None:raise TransferConflict()
        verify_probe_snapshot(intent,snapshot,require_stopped=True)
        if selection_fingerprint(intent,snapshot.piece_size)!=fingerprint:
            raise TransferConflict()
        probe=intent.candidate.probe_index
        if action=='skip':
            indices=[i for i in range(len(snapshot.priorities)) if i!=probe]
            priority=0
        else:
            indices=[probe]
            priority=1
        permit.take(fingerprint,action)
        if indices:
            self._request('torrents/filePrio',form={
                'hash':intent.inventory.info_hash,
                'id':'|'.join(map(str,indices)),
                'priority':priority,
            },limit=4096)

    def start_probe(self,intent,fingerprint,permit):
        """Start only when E01 is the exact sole wanted file."""
        from src.anime.acquisition.transfers import TransferPermit, TransferConflict
        from src.anime.batch.probe_transfer import (
            verify_probe_snapshot, exact_probe_selection, selection_fingerprint,
        )
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(permit) is not TransferPermit
                or self.options!=intent.options):
            raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.inventory.info_hash)
        if snapshot is None:raise TransferConflict()
        verify_probe_snapshot(intent,snapshot,require_stopped=True)
        if (selection_fingerprint(intent,snapshot.piece_size)!=fingerprint
                or not exact_probe_selection(intent,snapshot)):
            raise TransferConflict()
        permit.take(fingerprint,'start')
        self._request(
            'torrents/start' if self.major==5 else 'torrents/resume',
            form={'hashes':intent.inventory.info_hash},limit=4096)

    def apply_audio_verified_unlock(self,intent,media,unlock,permit):
        """Enable only other provider-approved trial files after real E01 proof."""
        from src.anime.acquisition.media import VerifiedMedia
        from src.anime.acquisition.transfers import TransferPermit, TransferConflict
        from src.anime.batch.unlock import (
            BatchUnlockIntent, plan_verified_unlock, validate_unlock_snapshot,
        )
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(media) is not VerifiedMedia
                or type(unlock) is not BatchUnlockIntent
                or type(permit) is not TransferPermit
                or self.options!=intent.options):
            raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.inventory.info_hash)
        if snapshot is None:raise TransferConflict()
        required=plan_verified_unlock(
            intent,snapshot,media,unlock.verification_job_id)
        if required!=unlock:raise TransferConflict()
        wanted=[i for i,p in enumerate(unlock.priorities)
                if p==1 and i!=intent.candidate.probe_index]
        if not wanted:raise TransferConflict()
        permit.take(unlock.fingerprint,'unlock')
        self._request('torrents/filePrio',form={
            'hash':intent.inventory.info_hash,
            'id':'|'.join(map(str,wanted)),
            'priority':1,
        },limit=4096)

    def start_audio_verified_batch(self,intent,unlock,permit):
        """Start a proven exact expanded selection, never an unverified pack."""
        from src.anime.acquisition.transfers import TransferPermit, TransferConflict
        from src.anime.batch.unlock import (
            BatchUnlockIntent, validate_unlock_snapshot, exact_unlock_selection,
        )
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(unlock) is not BatchUnlockIntent
                or type(permit) is not TransferPermit
                or self.options!=intent.options
                or unlock.plan_id!=intent.candidate.plan_id):
            raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.inventory.info_hash)
        if snapshot is None:raise TransferConflict()
        validate_unlock_snapshot(intent,snapshot,stopped=True)
        if not exact_unlock_selection(unlock,snapshot):raise TransferConflict()
        permit.take(unlock.fingerprint,'start')
        self._request(
            'torrents/start' if self.major==5 else 'torrents/resume',
            form={'hashes':intent.inventory.info_hash},limit=4096)

    def stop_completed_probe(self,intent,fingerprint,permit):
        """One irreversible stop send; never replay after network uncertainty."""
        from src.anime.acquisition.transfers import (
            TransferPermit, TransferConflict, ACTIVE,
        )
        from src.anime.batch.probe_transfer import (
            verify_probe_snapshot, exact_probe_selection,
        )
        import hashlib
        if (not self._sid or self.major not in (4,5)
                or type(intent) is not BatchSubmissionIntent
                or type(permit) is not TransferPermit
                or self.options!=intent.options):
            raise ClientInvalid()
        snapshot=self.inspect_transfer(intent.inventory.info_hash)
        if snapshot is None:raise TransferConflict()
        verify_probe_snapshot(intent,snapshot)
        probe=intent.candidate.probe_index
        calculated=hashlib.sha256(
            ('batch-probe-stop:'+intent.fingerprint+':'+
             str(snapshot.piece_size)+':'+str(probe)).encode()).hexdigest()
        if (calculated!=fingerprint or snapshot.progress[probe]!=1
                or not exact_probe_selection(intent,snapshot)
                or snapshot.observation.state not in ACTIVE):
            raise TransferConflict()
        permit.take(fingerprint,'stop')
        self._request(
            'torrents/stop' if self.major==5 else 'torrents/pause',
            form={'hashes':intent.inventory.info_hash},limit=4096)
