"""Offline geometry, bounded snapshots and exact transfer controls; unexecuted."""
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from src.nextgen.acquisition.metadata import parse_metainfo
from src.nextgen.acquisition.settings import DownloadOptions, DownloadCredentials
from src.nextgen.acquisition.submission import Intent, SubmissionPlan, Observation, ClientInvalid
from src.nextgen.acquisition.transfers import (TransferSnapshot, TransferPermit, TransferConflict,
    PieceOverlap, SnapshotChanged, verify_snapshot, transfer_state, selection_fingerprint)
from src.nextgen.torrent_client.qbittorrent import Client
from src.nextgen.torrent_client.qbittorrent_versions import select_adapter
from src.nextgen.tests.test_acquisition_metadata import metadata


def example(*,sidecar=0,video=100,piece=262144):
    files=None if not sidecar else [{b'length':sidecar,b'path':[b'info.nfo']},
                                   {b'length':video,b'path':[b'Test Anime - 10.mkv']}]
    total=sidecar+video
    raw,digest=metadata(name=b'Folder' if files else b'Test Anime - 10.mkv',files=files,
        extra={b'piece length':piece,b'pieces':b'p'*(20*((total+piece-1)//piece)),
               **({b'length':video} if not files else {})})
    manifest=parse_metainfo(raw,digest)
    plan=SubmissionPlan(10,20,30,40,'a'*32,manifest)
    options=DownloadOptions('https://qb.example','/srv/downloads/ad-'+plan.marker,'anime-discovery',30)
    intent=Intent(plan,options,'OWNED_STOPPED','f'*64,5)
    observation=Observation(digest,'stoppedDL',options.download_root,options.category,
                            ('ad-'+plan.marker,),False,manifest.files)
    offset=0;ranges=[]
    for f in manifest.files:
        ranges.append((offset//piece,(offset+f.size-1)//piece));offset+=f.size
    return intent,TransferSnapshot(observation,(1,)*len(manifest.files),(0,)*len(manifest.files),piece,tuple(ranges))


class TransferPolicyTests(unittest.TestCase):
    def test_single_file_and_aligned_sidecar_have_no_unwanted_boundary_bytes(self):
        for options in ({},{'sidecar':262144}):
            intent,snapshot=example(**options)
            self.assertIs(verify_snapshot(intent,snapshot),snapshot)
        intent,snapshot=example(sidecar=10)
        self.assertIs(verify_snapshot(intent,snapshot),snapshot)
        intent,snapshot=example(sidecar=262144,video=262145)
        self.assertIs(verify_snapshot(intent,snapshot),snapshot)  # Last partial piece is video only.

    def test_changed_ownership_inventory_and_piece_ranges_are_rejected(self):
        intent,snapshot=example()
        for changes in ({'save_path':'/elsewhere'},{'tags':()},{'auto_tmm':True},
                        {'category':'other'},{'state':'missingFiles'}, {'files':(replace(snapshot.observation.files[0],path='changed.mkv'),)}):
            with self.subTest(changes=changes),self.assertRaises(TransferConflict):
                verify_snapshot(intent,replace(snapshot,observation=replace(snapshot.observation,**changes)))
        with self.assertRaises(TransferConflict):verify_snapshot(intent,replace(snapshot,piece_ranges=((0,1),)))

    def test_progress_priorities_and_ranges_are_bounded(self):
        _,snapshot=example()
        for changes in ({'progress':(float('nan'),)},{'progress':(True,)},{'progress':(1.1,)},
                        {'priorities':(2,)},{'piece_size':0},{'piece_ranges':((True,0),)},
                        {'piece_ranges':((-2,0),)}):
            with self.subTest(changes=changes),self.assertRaises(ClientInvalid):replace(snapshot,**changes)

    def test_client_completion_is_selected_file_progress_with_exact_priorities(self):
        intent,snapshot=example(sidecar=262144)
        snapshot=replace(snapshot,priorities=(0,1),progress=(0,1))
        self.assertEqual(transfer_state(intent,snapshot),('COMPLETE','TORRENT_DOWNLOAD_COMPLETE'))
        with self.assertRaises(TransferConflict):transfer_state(intent,replace(snapshot,priorities=(1,1)))
        self.assertEqual(transfer_state(intent,replace(snapshot,progress=(0,.4),
            observation=replace(snapshot.observation,state='downloading')))[0],'ACTIVE')

    def client(self,intent,snapshot,major=5):
        client=Client(intent.options,DownloadCredentials('user','password'))
        client.major=major
        client.protocol=select_adapter(major,(2,11,0) if major==5 else (2,8,19))
        client._sid="offline-session"
        client.inspect_transfer=Mock(return_value=snapshot)
        client._request=Mock(return_value=(b'',None))
        return client

    def test_fixed_priority_ids_and_normal_start_are_one_shot(self):
        intent,snapshot=example(sidecar=262144)
        client=self.client(intent,snapshot)
        fingerprint=selection_fingerprint(intent,snapshot.piece_size)
        permit=TransferPermit(fingerprint,('skip','wanted'))
        client.apply_selection(intent,fingerprint,permit,'skip')
        client._request.assert_called_once_with('torrents/filePrio',form={'hash':intent.plan.manifest.info_hash,'id':'0','priority':0},limit=4096)
        client.apply_selection(intent,fingerprint,permit,'wanted')
        with self.assertRaises(TransferConflict):client.apply_selection(intent,fingerprint,permit,'skip')
        for major,endpoint in ((4,'torrents/resume'),(5,'torrents/start')):
            client=self.client(intent,replace(snapshot,priorities=(0,1)),major)
            permit=TransferPermit(fingerprint,('start',))
            client.start_selected(intent,fingerprint,permit)
            client._request.assert_called_once_with(endpoint,form={'hashes':intent.plan.manifest.info_hash},limit=4096)
            with self.assertRaises(TransferConflict):client.start_selected(intent,fingerprint,permit)

    def test_active_changed_selection_and_overlap_never_start(self):
        for options in ({'sidecar':10},{'sidecar':262144},{}):
            intent,snapshot=example(**options)
            if not options:snapshot=replace(snapshot,observation=replace(snapshot.observation,state='downloading'))
            client=self.client(intent,snapshot)
            with self.assertRaises((TransferConflict,PieceOverlap)):
                client.start_selected(intent,selection_fingerprint(intent,snapshot.piece_size),
                                      TransferPermit(selection_fingerprint(intent,snapshot.piece_size),('start',)))
            client._request.assert_not_called()

    def test_detailed_inspection_validates_stability_and_response_shape(self):
        intent,snapshot=example()
        file=intent.plan.manifest.files[0]
        details=[{'index':0,'name':file.path,'size':file.size,'priority':1,'progress':.2,'piece_range':[0,0]}]
        client=self.client(intent,snapshot)
        del client.inspect_transfer  # Exercise the real method with fake reads.
        client.inspect=Mock(return_value=snapshot.observation)
        client._request=Mock(side_effect=[(json.dumps(details).encode(),None),(b'{"piece_size":262144}',None)])
        self.assertEqual(client.inspect_transfer(file_hash:=intent.plan.manifest.info_hash).progress,(.2,))
        client.inspect=Mock(side_effect=[snapshot.observation,replace(snapshot.observation,state='downloading')])
        client._request=Mock(side_effect=[(json.dumps(details).encode(),None),(b'{"piece_size":262144}',None)])
        with self.assertRaises(SnapshotChanged):client.inspect_transfer(file_hash)
        client.inspect=Mock(return_value=snapshot.observation)
        details[0]['progress']=2
        client._request=Mock(side_effect=[(json.dumps(details).encode(),None),(b'{"piece_size":262144}',None)])
        with self.assertRaises(ClientInvalid):client.inspect_transfer(file_hash)
