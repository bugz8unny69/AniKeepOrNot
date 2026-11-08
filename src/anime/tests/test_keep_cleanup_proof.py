"""Focused contract tests for shared post-Keep proof and recovery lock."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.anime.keep_cleanup_proof import staged_keep, permanent_keep
from src.anime.sonarr import KeepReview
from src.anime.batch_post_keep_cleanup import BatchPostKeepCleanup


class KeepCleanupProofTests(unittest.TestCase):
    def setUp(self):
        self.data={
            'selection':dict(anidb_id=123,anidb_episode_id=5,tvdb_series_id=456,
                             tvdb_episode_id=8,tvdb_season=1,tvdb_episode=2,
                             preparation_plan_id=10,staging_root='/tmp/staged',
                             media_relative_path='episode.mkv'),
            'root_identity':[2,20], 'source_file':[2,30,100,10,20],
            'source_parent':[2,20],
            'series_id':99, 'series_path':'/tmp/permanent/Anime',
            'episode_id':11,
            'result':{'episode_file_id':25,'relative_path':'Anime/episode.mkv',
                      'size':100},
        }
        self.entries=[
            {'role':'media','path':'episode.mkv'},
            {'role':'episode','path':'episode.nfo'},
            {'role':'series','path':'tvshow.nfo'},
        ]
        self.proofs=[
            {'role':'media','path':'episode.mkv','root':[2,20],
             'file':[2,30,100,10,20],'hash_checked':True},
            {'role':'episode','path':'episode.nfo'},
            {'role':'series','path':'tvshow.nfo'},
        ]
        self.plan={'root':'/tmp/staged','root_identity':[2,20],
                   'entries':self.entries,'transfer':'COPY'}

    def checked(self, *, copied):
        return staged_keep(json.dumps(self.plan),json.dumps(self.proofs),
                           self.data,10,copy_only=copied)

    def test_valid_single_and_copy_only_batch_use_one_staging_proof(self):
        self.assertEqual(self.checked(copied=False).tvdb_episode,2)
        self.assertEqual(self.checked(copied=True).tvdb_episode,2)

    def test_bad_proof_or_source_binding_blocks_cleanup(self):
        cases=[
            lambda:self.proofs.pop(),
            lambda:self.proofs[0].update(file=[2,30,99,10,20]),
            lambda:self.proofs[1].update(path='different.nfo'),
            lambda:self.plan.update(root_identity=[9,9]),
            lambda:self.plan.update(transfer='MOVE'),
        ]
        for mutate in cases:
            with self.subTest(index=cases.index(mutate)):
                self.setUp()
                mutate()
                with self.assertRaises(KeepReview):
                    self.checked(copied=True)

    def test_copy_only_accepts_media_with_hash_checked_false(self):
        self.proofs[0]['hash_checked']=False
        self.assertEqual(self.checked(copied=True).tvdb_episode,2)

    def test_single_does_not_require_batch_copy_receipt(self):
        self.proofs[0]['hash_checked']=False
        self.plan.pop('transfer')
        self.assertEqual(self.checked(copied=False).tvdb_episode,2)

    def test_permanent_check_reloads_current_sonarr_episode_each_time(self):
        calls=[]
        class Sonarr:
            def series(self,series_id):
                calls.append('series')
                return {'id':99,'path':'/tmp/permanent/Anime'}
            def episode(self,selection,series):
                calls.append('episode')
                return {'id':11,'hasFile':True,'episodeFileId':25}
            def request(self,route):
                calls.append(route)
                return {'id':25}
        with patch('src.anime.keep_cleanup_proof.existing_file',
                   return_value=self.data['result']) as proof:
            permanent_keep(Sonarr(),
                           self.checked(copied=True),object(),self.data)
            self.assertEqual(calls,['series','episode','episodefile/25'])
            self.assertEqual(proof.call_count,1)

        class ReboundSonarr(Sonarr):
            def episode(self,selection,series):
                return {'id':11,'hasFile':True,'episodeFileId':777}
        with patch('src.anime.keep_cleanup_proof.existing_file') as proof:
            with self.assertRaises(KeepReview):
                permanent_keep(ReboundSonarr(),self.checked(copied=True),
                               object(),self.data)
            proof.assert_not_called()

    def test_batch_cleanup_uses_same_lock_as_single_cleanup(self):
        held=[]
        class Lock:
            def __enter__(self):
                held.append('acquired')
            def __exit__(self,*args):
                held.append('released')
        service=BatchPostKeepCleanup.__new__(BatchPostKeepCleanup)
        service.queue=SimpleNamespace(database='/tmp/not-used.sqlite')
        claim=SimpleNamespace(job_id=21)
        with (patch('src.anime.batch_post_keep_cleanup.StageJournal') as journal,
              patch.object(BatchPostKeepCleanup,'_run',return_value=True) as execute):
            journal.return_value.lock.return_value=Lock()
            self.assertTrue(service.run(claim,12,object()))
            execute.assert_called_once()
        self.assertEqual(held,['acquired','released'])
