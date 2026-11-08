"""Provider-backed, read-only batch verification regression coverage."""
import unittest

from src.anime.acquisition.metadata import BatchInventory, ObservedBatchInventory, TorrentFile
from src.anime.batch.verification import verify_batch_files, plan_provider_verified_batch
from src.anime.batch.window import plan_batch_user_evaluation_window
from src.anime.user_evaluation_policy import Window
from src.anime.providers.http import Credentials
from src.anime.release_contracts import ReleaseNotReady, ReleaseConflict
from src.anime.batch.selection import BatchPlanningError


def inventory(*paths):
    return BatchInventory('a'*40, 'b'*64,
                          tuple(TorrentFile(i,p,100) for i,p in enumerate(paths)))


class FakeClient:
    def __init__(self, *, season=1, offset=0):
        self.season, self.offset = season, offset
        self.catalog = {i:1000+i for i in range(1,13)}
        self.records = tuple((2000+i,season,i+offset) for i in range(1,13))
        self.pages = []
    def episodes(self, aid):
        assert aid == 123
        return self.catalog
    def mapping_xml(self):
        return ('<anime-list><anime anidbid="123" tvdbid="456" '
                'defaulttvdbseason="%s" episodeoffset="%s"/></anime-list>' %
                (self.season, self.offset)).encode()
    def episode_page(self, tid, page, credentials):
        assert tid == 456
        self.pages.append(page)
        return (self.records,False) if page == 0 else ((),False)


class ProviderBatchTests(unittest.TestCase):
    def check(self, files, client, season=1):
        return verify_batch_files(files,client=client,credentials=Credentials('secret'),
                                  anidb_id=123,tvdb_series_id=456,season=season)

    def test_older_qb_observed_batch_still_requires_provider_proof(self):
        observed=ObservedBatchInventory('a'*40,(
            TorrentFile(0,'Show/Season 01/Show S01E01.mkv',100),
            TorrentFile(1,'Show/Season 01/Show S01E02.mkv',200)))
        verified=self.check(observed,FakeClient())
        self.assertIsNone(verified.info_sha256)
        self.assertEqual(len(verified.files),2)
        plan=plan_provider_verified_batch(observed,verified)
        self.assertEqual(plan.probe_index,0)
        self.assertEqual(plan.episode_indices,((1,0),(2,1)))

    def test_shiki_regular_season_and_ova_has_one_to_one_provider_proofs(self):
        files = inventory(*tuple(f'Shiki/Season 01/Shiki S01E{i:02d}.mkv'
                                 for i in range(1,13)),
                          'Shiki/OVA/Shiki OVA 01.mkv','Shiki/info.nfo')
        client = FakeClient()
        verified=self.check(files,client)
        self.assertEqual(len(verified.files),12)
        self.assertEqual((verified.files[0].anidb_episode_id,
                          verified.files[0].tvdb_episode_id),(1001,2001))
        self.assertEqual(client.pages,[0])
        plan=plan_provider_verified_batch(files,verified)
        self.assertEqual(plan.probe_index,0)
        self.assertEqual(plan.remaining_indices,(1,2,3,4,5))
        self.assertIn(12,plan.extra_indices)
        self.assertEqual(plan.skipped_indices,tuple(range(6,14)))

    def test_anidb_offset_is_explicit_and_no_tvdb_episode_is_guessed(self):
        files=inventory('Show/Season 02/Show S02E13.mkv',
                        'Show/Season 02/Show S02E14.mkv')
        verified=self.check(files,FakeClient(season=2,offset=12),season=2)
        self.assertEqual(verified.episode_map(),{13:13,14:14})
        self.assertEqual(verified.files[0].anidb_episode_number,1)
        plan=plan_provider_verified_batch(files,verified,start_episode=13)
        self.assertEqual(plan.episode_indices,((13,0),(14,1)))
        with self.assertRaises(BatchPlanningError):
            plan_provider_verified_batch(
                inventory('Other/Season 02/Show S02E13.mkv'),verified,
                start_episode=13)

    def test_unqualified_number_uses_only_unique_interpretation(self):
        files=inventory('Show/Show - 01.mkv')
        verified=self.check(files,FakeClient(season=2,offset=12),season=2)
        self.assertEqual(verified.episode_map(),{1:13})
        self.assertEqual(plan_provider_verified_batch(files,verified,start_episode=13).probe_index,0)
        # Bare episode 13 also exists in AniDB catalog in this fixture if
        # catalog is expanded; differing forward and reverse readings fail.
        client=FakeClient(season=2,offset=12)
        client.catalog[13]=1013
        client.records += ((2013,2,25),)
        with self.assertRaises(ReleaseNotReady):
            self.check(inventory('Show/Show - 13.mkv'),client,season=2)

    def test_missing_tvdb_and_non_unique_anidb_mapping_block(self):
        client=FakeClient()
        client.records=()
        with self.assertRaises(ReleaseNotReady) as caught:
            self.check(inventory('Show/Show S01E01.mkv'),client)
        self.assertEqual(caught.exception.reason_code,'RELEASE_TVDB_EPISODE_MISSING')
        client=FakeClient()
        client.catalog[13]=1013
        client.records += ((2013,1,13),)
        client.mapping_xml=lambda: (
            b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1">'
            b'<mapping-list><mapping anidbseason="1" tvdbseason="1">'
            b'13-1;</mapping></mapping-list></anime></anime-list>')
        with self.assertRaises(ReleaseConflict):
            self.check(inventory('Show/Show S01E01.mkv'),client)

    def test_reject_cross_season_unknown_video_and_duplicate_mapping(self):
        for names in (('Show/Show S02E01.mkv',),
                      ('Show/Show S01E01.mkv','Show/Show S01E01 alternate.mkv'),
                      ('Show/Unknown.mkv',)):
            expected=ReleaseConflict if len(names)>1 else BatchPlanningError
            with self.subTest(names=names),self.assertRaises(expected):
                self.check(inventory(*names),FakeClient())

    def test_custom_user_evaluation_window_intersects_existing_batch_proofs(self):
        files=inventory(*tuple(f'Shiki/Season 01/Shiki S01E{i:02d}.mkv'
                                for i in range(1,13)),
                        'Shiki/OVA/Shiki OVA 01.mkv')
        verified=self.check(files,FakeClient())
        plan=plan_batch_user_evaluation_window(files,verified,window=Window(6,1),
                                     anchor_episode=10)
        self.assertEqual(plan.eligible_episodes,(1,2,3,4,5,6,10))
        self.assertEqual(plan.present_episodes,
                         ((1,0),(2,1),(3,2),(4,3),(5,4),(6,5),(10,9)))
        self.assertEqual(plan.probe_index,0)
        self.assertEqual(plan.remaining_indices,(1,2,3,4,5,9))
        self.assertIn(12,plan.skipped_indices)
        following=plan_batch_user_evaluation_window(files,verified,window=Window(0,2),
                                          anchor_episode=10)
        self.assertEqual(following.present_episodes,((10,9),(11,10)))
        self.assertEqual(following.probe_index,9)
        with self.assertRaises(BatchPlanningError):
            plan_batch_user_evaluation_window(files,verified,window=Window(0,1),
                                    anchor_episode=13)

    def test_verified_user_evaluation_plans_reject_omitted_regular_file_proofs(self):
        from dataclasses import replace
        files=inventory('Shiki/Season 01/Shiki S01E01.mkv',
                        'Shiki/Season 01/Shiki S01E02.mkv')
        verified=self.check(files,FakeClient())
        incomplete=replace(verified,files=verified.files[:1])
        with self.assertRaises(BatchPlanningError):
            plan_batch_user_evaluation_window(files,incomplete,window=Window(6,1),
                                    anchor_episode=1)
