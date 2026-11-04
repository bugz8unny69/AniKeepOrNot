"""Offline tests of batch-only stopped-add qB adapter and one-shot token."""
import unittest

from src.nextgen.acquisition.metadata import parse_batch_metainfo
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import ClientInvalid, Observation
from src.nextgen.acquisition.transfers import TransferSnapshot, TransferPermit, TransferConflict, PieceOverlap
from src.nextgen.batch.probe_transfer import (
    selection_fingerprint, verify_probe_snapshot, exact_probe_selection,
)
from src.nextgen.batch.qb_client import BatchClient
from src.nextgen.storage.batch_acquisition_candidate import BatchAcquisitionCandidate
from src.nextgen.storage.batch_submission_journal import (
    BatchSubmissionIntent, BatchSendPermit,
)
from src.nextgen.tests.test_acquisition_metadata import metadata


class BatchQbClientTests(unittest.TestCase):
    def setUp(self):
        files=[
            {b'length':100,b'path':[b'Season 01',b'Shiki S01E01.mkv']},
            {b'length':100,b'path':[b'Season 01',b'Shiki S01E02.mkv']},
            {b'length':100,b'path':[b'OVA',b'Shiki OVA 01.mkv']},
        ]
        self.raw,digest=metadata(name=b'Shiki',files=files)
        inventory=parse_batch_metainfo(self.raw,digest)
        self.options=DownloadOptions('http://localhost:8080',
                                     '/tmp/anime/ad-'+'a'*32,'trial',10)
        candidate=BatchAcquisitionCandidate(21,22,digest,inventory.info_sha256,
                                            0,((1,0),(2,1)),(2,),200)
        self.intent=BatchSubmissionIntent(candidate,inventory,self.options,
                                          'a'*32,'UNCERTAIN','b'*64,5)
        self.calls=[]
        self.client=BatchClient.__new__(BatchClient)
        self.client._sid='private-session'
        self.client.major=5
        self.client.options=self.options
        self.client._request=lambda path,**kwargs:self.calls.append((path,kwargs))

    def test_observed_batch_metadata_is_distinct_and_double_checked(self):
        from src.nextgen.acquisition.metadata import ObservedBatchInventory
        from src.nextgen.acquisition.qbittorrent import _Protocol
        from unittest.mock import Mock
        self.client.protocol=_Protocol(4,(2,8,19))
        self.client.major=4
        self.client.inspect=Mock()
        expected=Observation(self.intent.inventory.info_hash,'pausedDL',
            self.options.download_root,self.options.category,
            ('ad-'+self.intent.marker,),False,self.intent.inventory.files)
        self.client.inspect.side_effect=[expected,expected]
        observed=self.client.inspect_owned_batch_metadata(
            self.intent.inventory.info_hash,self.intent.marker,
            self.options.download_root)
        self.assertIs(type(observed),ObservedBatchInventory)
        self.assertEqual(observed.files,self.intent.inventory.files)
        self.assertIsNone(observed.info_sha256)
        self.assertEqual(self.client.inspect.call_count,2)
        self.assertEqual(self.calls,[])  # No mutations

    def test_adopted_active_batch_supports_read_only_inventory_rechecks(self):
        from unittest.mock import Mock
        from src.nextgen.acquisition.qbittorrent import _Protocol
        from src.nextgen.acquisition.metadata import ObservedBatchInventory
        self.client.protocol=_Protocol(4,(2,8,19))
        self.client.major=4
        active=Observation(self.intent.inventory.info_hash,'downloading',
            self.options.download_root,self.options.category,
            ('ad-'+self.intent.marker,),False,self.intent.inventory.files)
        self.client.inspect=Mock(return_value=active)
        self.assertIsNone(self.client.inspect_owned_batch_metadata(
            self.intent.inventory.info_hash,self.intent.marker,
            self.options.download_root))
        actual=self.client.inspect_owned_batch_metadata(
            self.intent.inventory.info_hash,self.intent.marker,
            self.options.download_root,require_stopped=False)
        self.assertIs(type(actual),ObservedBatchInventory)
        self.assertEqual(actual.files,self.intent.inventory.files)
        self.assertIsNone(actual.info_sha256)

    def test_observed_batch_never_adopts_foreign_or_changed_torrent(self):
        from src.nextgen.acquisition.qbittorrent import _Protocol
        from unittest.mock import Mock
        self.client.protocol=_Protocol(4,(2,8,19))
        self.client.major=4
        expected=Observation(self.intent.inventory.info_hash,'pausedDL',
            self.options.download_root,self.options.category,
            ('ad-'+self.intent.marker,),False,self.intent.inventory.files)
        wrong=Observation(self.intent.inventory.info_hash,'pausedDL',
            self.options.download_root,self.options.category,
            ('ad-'+('b'*32),),False,self.intent.inventory.files)
        self.client.inspect=Mock(return_value=wrong)
        self.assertIsNone(self.client.inspect_owned_batch_metadata(
            self.intent.inventory.info_hash,self.intent.marker,
            self.options.download_root))
        self.client.inspect=Mock(side_effect=[expected,wrong])
        with self.assertRaises(ClientInvalid):
            self.client.inspect_owned_batch_metadata(
                self.intent.inventory.info_hash,self.intent.marker,
                self.options.download_root)
        self.assertEqual(self.calls,[])

    def test_older_protocol_one_use_batch_magnet_admission(self):
        from src.nextgen.acquisition.qbittorrent import _Protocol
        from src.nextgen.storage.batch_metadata_admission import (
            EarlyBatchIntent, EarlyBatchPermit)
        digest=self.intent.inventory.info_hash
        magnet='magnet:?xt=urn:btih:'+digest
        for major,version in ((4,(2,8,19)),(4,(2,8,15)),(5,(2,10,0))):
            with self.subTest(major=major,api=version):
                self.calls.clear()
                self.client.major=major
                self.client.api_version=version
                self.client.protocol=_Protocol(major,version)
                early=EarlyBatchIntent(7,21,digest,'a'*32,self.options,major,
                                       'UNCERTAIN')
                permit=EarlyBatchPermit(7,'a'*32)
                self.client.add_early_batch_magnet(magnet,early,permit)
                self.assertEqual(self.calls[0][0],'torrents/add')
                fields=self.calls[0][1]['form']
                self.assertEqual(fields['paused'],'true')
                self.assertEqual(fields['autoTMM'],'false')
                self.assertEqual(fields['tags'],'ad-'+'a'*32)
                with self.assertRaises(ClientInvalid):
                    self.client.add_early_batch_magnet(magnet,early,permit)
                self.assertEqual(len(self.calls),1)

    def test_immutable_metadata_and_stopped_only_fields(self):
        permit=BatchSendPermit(self.intent.fingerprint)
        self.client.add_batch_stopped(self.raw,self.intent,permit)
        self.assertEqual(len(self.calls),1)
        path,kwargs=self.calls[0]
        self.assertEqual(path,'torrents/add')
        self.assertIn(b'name="stopped"',kwargs['body'])
        self.assertIn(b'name="autoTMM"',kwargs['body'])
        self.assertIn(b'ad-'+'a'.encode()*32,kwargs['body'])
        self.assertNotIn(b'name="filePrio"',kwargs['body'])
        with self.assertRaises(ClientInvalid):
            self.client.add_batch_stopped(self.raw,self.intent,permit)
        self.assertEqual(len(self.calls),1)

    def test_changed_or_unexpected_metadata_does_not_consume_permit(self):
        permit=BatchSendPermit(self.intent.fingerprint)
        with self.assertRaises(ClientInvalid):
            self.client.add_batch_stopped(self.raw+b'junk',self.intent,permit)
        self.assertEqual(self.calls,[])
        self.client.add_batch_stopped(self.raw,self.intent,permit)
        self.assertEqual(len(self.calls),1)


    def snapshot(self,*,priorities=(1,1,1),ranges=((0,0),(1,1),(2,2)),
                 state='stoppedDL',tags=None,piece_size=100):
        observed=Observation(self.intent.inventory.info_hash,state,
            self.options.download_root,self.options.category,
            ('ad-'+self.intent.marker,) if tags is None else tags,
            False,self.intent.inventory.files)
        return TransferSnapshot(observed,tuple(priorities),(0.,0.,0.),
                                piece_size,tuple(ranges))

    def test_probe_skip_wanted_start_each_single_use(self):
        current=[self.snapshot()]
        self.client.inspect_transfer=lambda _:current[0]
        fingerprint=selection_fingerprint(self.intent,100)
        permit=TransferPermit(fingerprint,('skip','wanted'))
        self.client.apply_probe_selection(self.intent,fingerprint,permit,'skip')
        self.client.apply_probe_selection(self.intent,fingerprint,permit,'wanted')
        self.assertEqual([p for p,_ in self.calls],
                         ['torrents/filePrio','torrents/filePrio'])
        with self.assertRaises(TransferConflict):
            self.client.apply_probe_selection(self.intent,fingerprint,permit,'skip')
        current[0]=self.snapshot(priorities=(1,0,0))
        self.assertTrue(exact_probe_selection(self.intent,current[0]))
        start=TransferPermit(fingerprint,('start',))
        self.client.start_probe(self.intent,fingerprint,start)
        self.assertEqual(self.calls[-1][0],'torrents/start')
        with self.assertRaises(TransferConflict):
            self.client.start_probe(self.intent,fingerprint,start)

    def test_overlap_and_wrong_owner_never_consume_probe_permission(self):
        with self.assertRaises(TransferConflict):
            verify_probe_snapshot(self.intent,self.snapshot(ranges=((0,1),(1,1),(2,2))),require_stopped=True)
        snap=self.snapshot(piece_size=150,ranges=((0,0),(0,1),(1,1)))
        self.assertIs(verify_probe_snapshot(self.intent,snap,require_stopped=True),snap)
        current=[self.snapshot(priorities=(1,0,0),tags=('another-owner',))]
        self.client.inspect_transfer=lambda _:current[0]
        fingerprint=selection_fingerprint(self.intent,100)
        permit=TransferPermit(fingerprint,('skip','wanted'))
        with self.assertRaises(TransferConflict):
            self.client.apply_probe_selection(self.intent,fingerprint,permit,'skip')
        self.assertEqual(self.calls,[])
        current[0]=self.snapshot()
        self.client.apply_probe_selection(self.intent,fingerprint,permit,'skip')
        self.assertEqual(len(self.calls),1)
        current[0]=self.snapshot(priorities=(1,0,0),tags=('another-owner',))
        start=TransferPermit(fingerprint,('start',))
        with self.assertRaises(TransferConflict):
            self.client.start_probe(self.intent,fingerprint,start)
        self.assertEqual(len(self.calls),1)


if __name__=='__main__':
    unittest.main()
