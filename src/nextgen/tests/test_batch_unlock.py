"""Offline proof-gated E01->approved-episodes unlock constraints."""
import unittest

from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile
from src.nextgen.acquisition.media import VerifiedMedia, MediaInvalid
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import Observation
from src.nextgen.acquisition.transfers import TransferSnapshot, TransferPermit, TransferConflict, PieceOverlap
from src.nextgen.batch.qb_client import BatchClient
from src.nextgen.batch.unlock import (
    plan_verified_unlock, planned_priorities, exact_unlock_selection,
)
from src.nextgen.storage.batch_acquisition_candidate import BatchAcquisitionCandidate
from src.nextgen.storage.batch_submission_journal import BatchSubmissionIntent


class AudioGatedUnlockTests(unittest.TestCase):
    def setUp(self):
        inventory=BatchInventory('a'*40,'b'*64,(
            TorrentFile(0,'Show/Season 01/Show S01E01.mkv',100),
            TorrentFile(1,'Show/Season 01/Show S01E02.mkv',100),
            TorrentFile(2,'Show/OVA/Show OVA 01.mkv',100)))
        self.options=DownloadOptions('http://localhost:8080',
                                    '/tmp/nextgen/ad-'+'c'*32,'trial',10)
        candidate=BatchAcquisitionCandidate(
            12,13,inventory.info_hash,inventory.info_sha256,0,
            ((1,0),(2,1)),(2,),200)
        self.intent=BatchSubmissionIntent(candidate,inventory,self.options,
                                          'c'*32,'OWNED_STOPPED','d'*64,5)
        self.media=VerifiedMedia(12,14,13,'d'*64,'e'*64,
            inventory.files[0].path,(1,2),(1,2,100,3,4),'f'*64,120000,
            ('eng','jpn'))
        self.calls=[]
        self.client=BatchClient.__new__(BatchClient)
        self.client._sid='authenticated'
        self.client.major=5
        self.client.options=self.options
        self.client._request=lambda path,**args:self.calls.append((path,args))
        self.current=[self.snapshot()]
        self.client.inspect_transfer=lambda _:self.current[0]

    def snapshot(self,*,priorities=(1,0,0),ranges=((0,0),(1,1),(2,2)),
                 state='stoppedDL',tags=None,piece_size=100):
        observed=Observation(self.intent.inventory.info_hash,state,
            self.options.download_root,self.options.category,
            ('ad-'+self.intent.marker,) if tags is None else tags,
            False,self.intent.inventory.files)
        return TransferSnapshot(observed,tuple(priorities),(1.,0.,0.),piece_size,tuple(ranges))

    def test_real_verified_probe_gates_one_time_file_unlock_and_start(self):
        plan=plan_verified_unlock(self.intent,self.snapshot(),self.media,15)
        self.assertEqual(planned_priorities(self.intent),(1,1,0))
        self.assertEqual(plan.priorities,(1,1,0))
        permit=TransferPermit(plan.fingerprint,('unlock',))
        self.client.apply_audio_verified_unlock(self.intent,self.media,plan,permit)
        self.assertEqual(self.calls[0][0],'torrents/filePrio')
        self.assertEqual(self.calls[0][1]['form']['id'],'1')
        with self.assertRaises(TransferConflict):
            self.client.apply_audio_verified_unlock(self.intent,self.media,plan,permit)
        self.current[0]=self.snapshot(priorities=(1,1,0))
        self.assertTrue(exact_unlock_selection(plan,self.current[0]))
        starter=TransferPermit(plan.fingerprint,('start',))
        self.client.start_audio_verified_batch(self.intent,plan,starter)
        self.assertEqual(self.calls[-1][0],'torrents/start')
        with self.assertRaises(TransferConflict):
            self.client.start_audio_verified_batch(self.intent,plan,starter)
        self.assertEqual(len(self.calls),2)

    def test_unverified_or_wrong_owner_never_sends(self):
        from dataclasses import replace
        with self.assertRaises(TransferConflict):
            plan_verified_unlock(self.intent,self.snapshot(ranges=((0,0),(0,1),(2,2))),self.media,15)
        with self.assertRaises(TransferConflict):
            plan_verified_unlock(
                self.intent,self.snapshot(),
                replace(self.media,relative_path='Other.mkv'),15)
        self.assertIsNotNone(plan_verified_unlock(
            self.intent,self.snapshot(piece_size=150,ranges=((0,0),(0,1),(1,1))),
            self.media,15))
        with self.assertRaises(TransferConflict):
            plan_verified_unlock(
                self.intent,self.snapshot(tags=('unowned',)),
                self.media,15)
        self.assertEqual(self.calls,[])

    def test_probe_stop_can_only_be_sent_once_after_e01_completion(self):
        import hashlib
        self.current[0]=self.snapshot(state='downloading')
        probe=self.intent.candidate.probe_index
        fp=hashlib.sha256(
            ('batch-probe-stop:'+self.intent.fingerprint+':'+
             str(self.current[0].piece_size)+':'+str(probe)).encode()).hexdigest()
        permit=TransferPermit(fp,('stop',))
        self.client.stop_completed_probe(self.intent,fp,permit)
        self.assertEqual(self.calls[0][0],'torrents/stop')
        with self.assertRaises(TransferConflict):
            self.client.stop_completed_probe(self.intent,fp,permit)
        self.assertEqual(len(self.calls),1)

    def test_probe_stop_rejects_wrong_priorities_and_unowned_files(self):
        import hashlib
        self.current[0]=self.snapshot(state='downloading',priorities=(1,1,0))
        fp=hashlib.sha256(
            ('batch-probe-stop:'+self.intent.fingerprint+':100:0').encode()).hexdigest()
        permit=TransferPermit(fp,('stop',))
        with self.assertRaises(TransferConflict):
            self.client.stop_completed_probe(self.intent,fp,permit)
        self.assertEqual(self.calls,[])

    def test_outside_trial_and_ova_are_always_priority_zero(self):
        plan=plan_verified_unlock(self.intent,self.snapshot(),self.media,15)
        self.assertEqual(plan.priorities[2],0)
        self.current[0]=self.snapshot(priorities=(1,1,1))
        start=TransferPermit(plan.fingerprint,('start',))
        with self.assertRaises(TransferConflict):
            self.client.start_audio_verified_batch(self.intent,plan,start)
        self.assertEqual(self.calls,[])


if __name__=='__main__':
    unittest.main()
