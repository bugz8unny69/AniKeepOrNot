"""Tests for moving TVDB dependency to Keep: graceful discovery and late resolution."""
import datetime
import unittest
import sqlite3
from unittest.mock import MagicMock

from src.nextgen.acquisition.prowlarr_search import EpisodeTarget
from src.nextgen.keep_resolution import resolve_tvdb_placement, make_keep_resolver
from src.nextgen.keep_selection import KeepSelection, KeepSelectionInvalid, load_selection
from src.nextgen.keep_service import Service
from src.nextgen.providers.backfill_catalog import build_catalog, make_catalog_factory
from src.nextgen.secrets import Credentials
from src.nextgen.storage.sqlite import initialize
from src.nextgen.storage.sonarr_backfill import load_batch, _plan


class FakeProviders:
    def __init__(self, *, mapping, tvdb, airdates, fail_tvdb=False):
        self._mapping = mapping
        self._tvdb = tvdb
        self._airdates = airdates
        self._fail_tvdb = fail_tvdb

    def anime(self, aid):
        return {'anidb_id': aid, 'title': 'Test Show', 'titles': ('Test Show',)}

    def episodes(self, aid):
        return {n: 5000 + n for n in self._airdates}

    def airdates(self, aid):
        return self._airdates

    def mapping_xml(self):
        if self._fail_tvdb:
            raise RuntimeError('Mapping network failure')
        return self._mapping

    def episode_page(self, tid, page, secret):
        if self._fail_tvdb:
            raise RuntimeError('TVDB API 500 error')
        return self._tvdb, False


class KeepResolutionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def _seed_staged_unmapped(self, db, *, aid=50, tid=100, ep_num=1, eid=5001):
        d64 = 'd' * 64
        a40 = 'a' * 40
        b64 = 'b' * 64
        c32 = 'c' * 32
        f64 = 'f' * 64
        db.executescript(f"""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'manual-release',X'61',1);
            INSERT INTO identified_anime VALUES({aid},{tid},'Test Show');
            INSERT INTO identity_results VALUES(1,1,{aid},2,'{{"anime":{{"anidb_id":{aid},"tvdb_series_id":{tid}}}}}',1);
            INSERT INTO identified_episodes VALUES({eid}, {aid});
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(10,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(1,10,1,{aid},'{d64}','DECLARED_SINGLE_EPISODE',{ep_num},{eid},{tid},NULL,NULL,NULL,1);
            INSERT INTO trial_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,tvdb_order,anchor_verification_id,anchor_episode,
              backfill_count,following_count,configuration_revision,created_at_ms)
              VALUES(1,{aid},{tid},1,'official',1,1,6,3,1,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(11,1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES(1,11,2,1,1,{aid},{eid},{tid},NULL,NULL,NULL,1);
            INSERT INTO sonarr_episode_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms)
              VALUES(1,{ep_num},NULL,NULL,NULL,'{a40}',1);
            INSERT INTO acquisition_plans(plan_id,job_id,reservation_id,info_hash,info_sha256,manifest_json,
              submission_marker,state,configuration_revision,prepared_at_ms)
              VALUES(1,11,1,'{a40}','{b64}','{{}}','{c32}','PREPARED',1,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(12,1,'DOWNLOAD_TORRENT','{{}}','DONE',0,0,0);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(13,1,'VERIFY_MEDIA','{{}}','DONE',0,0,0);
            INSERT INTO media_verifications(plan_id,job_id,download_job_id,submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms)
              VALUES(1,13,12,'{f64}','{f64}','{{"path":"s/S01E01.mkv"}}',1);
            INSERT INTO job_operations(operation_id,state,created_at_ms,updated_at_ms) VALUES(20,'STAGED',1,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(14,1,'STAGE_TRIAL_MEDIA','{{}}','DONE',0,0,0);
            INSERT INTO job_operation_members(job_id,operation_id) VALUES(14,20);
            INSERT INTO staging_publications(plan_id,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms)
              VALUES(1,14,13,'{{"entries":[{{"role":"media","path":"s/S01E01.mkv"}}],"root":"/staging"}}',
                     '[{{"role":"media","path":"s/S01E01.mkv"}}]',
                     'PUBLISHED',1,1);
        """)
        db.commit()

    def test_discovery_catalog_succeeds_without_tvdb_placement(self):
        # Even if TVDB API raises 500 or network fails, AniDB aired episodes are SEARCHABLE
        providers = FakeProviders(
            mapping=b'', tvdb=(),
            airdates={1: '2026-10-01', 2: '2026-10-08', 3: '2026-12-01'},
            fail_tvdb=True)
        catalog = build_catalog(providers, 50, 100, None, today=datetime.date(2026, 10, 29))
        self.assertEqual(catalog.titles, ('Test Show',))
        # Aired episodes are SEARCHABLE with None placement
        self.assertEqual(catalog.target(1), EpisodeTarget(None, 'SEARCHABLE', 1))
        self.assertEqual(catalog.target(2), EpisodeTarget(None, 'SEARCHABLE', 2))
        # Future episode is NOT_AIRED
        self.assertEqual(catalog.target(3), EpisodeTarget(None, 'NOT_AIRED', 3))

    def test_reservation_tracks_unmapped_active_episodes_during_backfill(self):
        self._seed_staged_unmapped(self.db, ep_num=1)
        b64 = 'b' * 64
        # Seed an active unmapped reservation for episode 2
        self.db.executescript(f"""
            INSERT INTO identified_episodes VALUES(5002, 50);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(20,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(2,20,1,50,'{b64}','DECLARED_SINGLE_EPISODE',2,5002,100,NULL,NULL,NULL,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(21,1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES(2,21,2,2,1,50,5002,100,NULL,NULL,NULL,1);
        """)
        self.db.commit()

        batch = _plan(self.db, 1, 1)
        self.assertIn(2, batch.reserved)

    def test_resolve_tvdb_placement_success(self):
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(
            mapping=mapping,
            tvdb=((9001, 1, 1),),
            airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        resolved = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertEqual(resolved, (9001, 1, 1))

        # Check DB updates:
        # release_verifications
        v = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM release_verifications WHERE verification_id=1").fetchone()
        self.assertEqual(v, (1, 1, 9001))

        # episode_reservations
        r = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_reservations WHERE reservation_id=1").fetchone()
        self.assertEqual(r, (1, 1, 9001))

        # sonarr_episode_selections
        s = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM sonarr_episode_selections WHERE plan_id=1").fetchone()
        self.assertEqual(s, (1, 1, 9001))

        # recognized_episodes
        rec = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM recognized_episodes WHERE anidb_id=50 AND anidb_episode_id=5001").fetchone()
        self.assertEqual(rec, (1, 1, 9001))

    def test_resolve_tvdb_placement_still_missing(self):
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        # Episode 1 is not in TVDB episode page
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(
            mapping=mapping,
            tvdb=(), # empty TVDB
            airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        resolved = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertIsNone(resolved)

        # DB remains unchanged
        v = self.db.execute("SELECT tvdb_episode_id FROM release_verifications WHERE verification_id=1").fetchone()
        self.assertIsNone(v[0])

    def test_load_selection_uses_resolver(self):
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)

        # Resolver resolves it:
        mock_resolver = MagicMock(return_value=(9001, 1, 1))
        selection = load_selection(self.db, 14, resolver=mock_resolver)
        self.assertEqual(selection.tvdb_episode_id, 9001)
        self.assertEqual(selection.tvdb_season, 1)
        self.assertEqual(selection.tvdb_episode, 1)
        mock_resolver.assert_called_once_with(self.db, 50, 5001, 100, 14)

        # Resolver fails to resolve:
        mock_failing_resolver = MagicMock(return_value=None)
        # reset DB row to NULL for test
        self.db.execute("DELETE FROM recognized_episodes")
        with self.assertRaises(KeepSelectionInvalid):
            load_selection(self.db, 14, resolver=mock_failing_resolver)


if __name__ == '__main__':
    unittest.main()
