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
from src.nextgen.storage.episode_discovery import load_batch, _plan


class FakeProviders:
    def __init__(self, *, mapping, tvdb, airdates, episodes=None, fail_tvdb=False):
        self._mapping = mapping
        self._tvdb = tvdb
        self._airdates = airdates
        self._episodes = episodes
        self._fail_tvdb = fail_tvdb

    def anime(self, aid):
        return {'anidb_id': aid, 'title': 'Test Show', 'titles': ('Test Show',)}

    def episodes(self, aid):
        if self._episodes is not None:
            return self._episodes
        return {n: 5000 + n for n in self._airdates}

    def airdates(self, aid):
        return self._airdates

    def mapping_xml(self):
        if self._fail_tvdb:
            from src.nextgen.providers.errors import ProviderUnavailable
            raise ProviderUnavailable()
        return self._mapping

    def episode_page(self, tid, page, secret):
        if self._fail_tvdb:
            from src.nextgen.providers.errors import ProviderUnavailable
            raise ProviderUnavailable()
        return self._tvdb, False


class KeepResolutionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def _seed_staged_unmapped(self, db, *, aid=50, tid=100, ep_num=1, eid=5001,
                              user_evaluation_plan_id=1, acq_plan_id=1, res_id=1,
                              verification_id=1, stage_job_id=14, op_id=20):
        d64 = f"{verification_id:064x}"
        a40 = f"{acq_plan_id:040x}"
        b64 = f"{acq_plan_id:064x}"
        c32 = f"{acq_plan_id:032x}"
        f64 = f"{acq_plan_id:064x}"
        job_base = 1000 * aid + verification_id * 10
        db.executescript(f"""
            INSERT OR IGNORE INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'manual-release',X'61',1);
            INSERT OR IGNORE INTO identified_anime VALUES({aid},{tid},'Test Show {aid}');
            INSERT OR IGNORE INTO identity_results VALUES({verification_id},1,{aid},2,'{{"anime":{{"anidb_id":{aid},"tvdb_series_id":{tid}}}}}',1);
            INSERT OR IGNORE INTO identified_episodes VALUES({eid}, {aid});
            INSERT OR IGNORE INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES({job_base + 10},1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES({verification_id},{job_base + 10},{verification_id},{aid},'{d64}','DECLARED_SINGLE_EPISODE',{ep_num},{eid},{tid},NULL,NULL,NULL,1);
            INSERT INTO user_evaluation_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,tvdb_order,anchor_verification_id,anchor_episode,
              backfill_count,following_count,configuration_revision,created_at_ms)
              VALUES({user_evaluation_plan_id},{aid},{tid},1,'official',{verification_id},{ep_num},6,3,1,1);
            INSERT OR IGNORE INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES({job_base + 11},1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES({res_id},{job_base + 11},2,{verification_id},{user_evaluation_plan_id},{aid},{eid},{tid},NULL,NULL,NULL,1);
            INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms)
              VALUES({user_evaluation_plan_id},{ep_num},NULL,NULL,NULL,'{a40}',1);
            INSERT INTO acquisition_plans(plan_id,job_id,reservation_id,info_hash,info_sha256,manifest_json,
              submission_marker,state,configuration_revision,prepared_at_ms)
              VALUES({acq_plan_id},{job_base + 11},{res_id},'{a40}','{b64}','{{}}','{c32}','PREPARED',1,1);
            INSERT OR IGNORE INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES({job_base + 12},1,'DOWNLOAD_TORRENT','{{}}','DONE',0,0,0);
            INSERT OR IGNORE INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES({job_base + 13},1,'VERIFY_MEDIA','{{}}','DONE',0,0,0);
            INSERT INTO media_verifications(plan_id,job_id,download_job_id,submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms)
              VALUES({acq_plan_id},{job_base + 13},{job_base + 12},'{f64}','{f64}','{{"path":"s/S01E01.mkv"}}',1);
            INSERT OR IGNORE INTO job_operations(operation_id,state,created_at_ms,updated_at_ms) VALUES({op_id},'STAGED',1,1);
            INSERT OR IGNORE INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES({stage_job_id},1,'STAGE_USER_EVALUATION_MEDIA','{{}}','DONE',0,0,0);
            INSERT OR IGNORE INTO job_operation_members(job_id,operation_id) VALUES({stage_job_id},{op_id});
            INSERT INTO staging_publications(plan_id,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms)
              VALUES({acq_plan_id},{stage_job_id},{job_base + 13},'{{"entries":[{{"role":"media","path":"s/S01E01.mkv"}}],"root":"/staging"}}',
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

        # episode_release_selections
        s = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=1").fetchone()
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

    def test_cross_anime_plan_isolation(self):
        """W-05: Resolving Anime A must NOT update Anime B with the same episode number."""
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)

        # Seed Anime B (aid=60, tid=200, ep_num=1, eid=6001, plan_id=2)
        d64 = 'e' * 64
        a40 = 'b' * 40
        self.db.executescript(f"""
            INSERT INTO identified_anime VALUES(60, 200, 'Other Show');
            INSERT INTO identity_results VALUES(2, 1, 60, 2, '{{"anime":{{"anidb_id":60,"tvdb_series_id":200}}}}', 1);
            INSERT INTO identified_episodes VALUES(6001, 60);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(30,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(3,30,2,60,'{d64}','DECLARED_SINGLE_EPISODE',1,6001,200,NULL,NULL,NULL,1);
            INSERT INTO user_evaluation_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,tvdb_order,anchor_verification_id,anchor_episode,
              backfill_count,following_count,configuration_revision,created_at_ms)
              VALUES(2,60,200,1,'official',3,1,6,3,1,1);
            INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms)
              VALUES(2,1,NULL,NULL,NULL,'{a40}',1);
        """)
        self.db.commit()

        # Both plans have NULL tvdb_episode_id for ep_num=1
        sel_a = self.db.execute("SELECT tvdb_episode_id FROM episode_release_selections WHERE plan_id=1").fetchone()
        sel_b = self.db.execute("SELECT tvdb_episode_id FROM episode_release_selections WHERE plan_id=2").fetchone()
        self.assertIsNone(sel_a[0])
        self.assertIsNone(sel_b[0])

        # Resolve Anime A's episode 1
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        resolved = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertEqual(resolved, (9001, 1, 1))

        # Check: Anime A is updated to (1, 1, 9001)
        sel_a_after = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=1").fetchone()
        self.assertEqual(sel_a_after, (1, 1, 9001))

        # Check: Anime B is UNTOUCHED!
        sel_b_after = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=2").fetchone()
        self.assertEqual(sel_b_after, (None, None, None))

    def test_provider_outage_raises_keep_unavailable(self):
        """W-06: Provider network/500 failure raises KeepUnavailable for automatic retry."""
        from src.nextgen.sonarr import KeepUnavailable
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        providers = FakeProviders(mapping=b'', tvdb=(), airdates={1: '2026-10-01'}, fail_tvdb=True)
        secret = Credentials('fake-key')

        with self.assertRaises(KeepUnavailable):
            resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)

    def test_unconfigured_credentials_raises_keep_configuration(self):
        """W-06: Unconfigured credentials raise KeepConfiguration for actionable user feedback."""
        from src.nextgen.sonarr import KeepConfiguration
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        providers = FakeProviders(mapping=b'', tvdb=(), airdates={1: '2026-10-01'})

        with self.assertRaises(KeepConfiguration):
            resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=None)

    def test_conflicting_recognized_placement_raises_keep_review(self):
        """W-06: Discovered placement that conflicts with existing recognized episode raises KeepReview."""
        from src.nextgen.sonarr import KeepReview
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        # Pre-seed conflicting recognized episode for aid=50, ep_num=1
        self.db.execute(
            "INSERT INTO recognized_seasons VALUES(100, 1, 50, 'Test Show', 1, 1)")
        self.db.execute(
            "INSERT INTO recognized_episodes VALUES(100, 1, 50, 1, 5001, 1, 9999, 1, 1)")
        self.db.commit()

        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        # Resolver tries to map to tvdb_eid 9001 instead of 9999
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        with self.assertRaises(KeepReview):
            resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)

    def test_repeat_resolution_is_idempotent(self):
        """W-06: Resolving the same placement repeatedly succeeds idempotently."""
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        first = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertEqual(first, (9001, 1, 1))

        # Repeat with same providers/secret
        second = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertEqual(second, (9001, 1, 1))

    def test_same_anime_multiple_plans_resolves_exact_staged_plan(self):
        """High 2: Resolves exact plan associated with staged acquisition when anime has multiple plans."""
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)

        # Seed plan 2 for the same anime via batch_staging_publications and episode_release_selections
        b40 = 'b' * 40
        self.db.execute(f"""
            INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms)
            VALUES(2, 1, NULL, NULL, NULL, '{b40}', 1)
        """)
        self.db.execute("""
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
            VALUES(15, 1, 'STAGE_BATCH_EPISODE', '{}', 'DONE', 0, 0, 0);
        """)
        self.db.execute("""
            INSERT INTO batch_staging_publications(plan_id,file_index,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms)
            VALUES(2, 0, 15, 13, '{"entries":[{"role":"media","path":"s/S01E01.mkv"}],"root":"/staging"}',
                   '[{"role":"media","path":"s/S01E01.mkv"}]', 'PUBLISHED', 1, 1);
        """)
        self.db.commit()

        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        # target_job_id=14 is associated with plan_id=1 in staging_publications
        resolved = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertEqual(resolved, (9001, 1, 1))

        # Verify plan 1 is updated
        sel_1 = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=1").fetchone()
        self.assertEqual(sel_1, (1, 1, 9001))

        # Verify plan 2 is UNTOUCHED
        sel_2 = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=2").fetchone()
        self.assertEqual(sel_2, (None, None, None))

    def test_unresolved_plan_raises_keep_review_without_guessing(self):
        """High 2: When staged acquisition cannot be linked to a plan, KeepReview is raised without guessing."""
        from src.nextgen.sonarr import KeepReview
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)

        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        # Remove the active reservation and provide an unknown target_job_id (999)
        self.db.execute("DELETE FROM episode_reservations")
        self.db.commit()

        with self.assertRaises(KeepReview):
            resolve_tvdb_placement(self.db, 50, 5001, 100, 999, client=providers, secret=secret)

    def test_unexpected_provider_error_propagates_without_being_disguised_as_missing_mapping(self):
        """Medium 4: Unexpected exceptions propagate rather than silently becoming None."""
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                   b'</anime></anime-list>')
        providers = FakeProviders(mapping=mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        # Mock episode_page to raise an unexpected ValueError (e.g., programming bug or corrupted state)
        providers.episode_page = MagicMock(side_effect=ValueError("corrupted internal provider state"))

        with self.assertRaises(ValueError) as ctx:
            resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)
        self.assertIn("corrupted internal provider state", str(ctx.exception))

    def test_mapping_conflict_raises_keep_review(self):
        """Medium 4: Conflicting Anime-Lists mapping raises KeepReview rather than returning None."""
        from src.nextgen.sonarr import KeepReview
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)
        # Mapping specifies tvdbid 999, which conflicts with source tvdbid 100
        conflict_mapping = b'<anime-list><anime anidbid="50" tvdbid="999" defaulttvdbseason="1"></anime></anime-list>'
        providers = FakeProviders(mapping=conflict_mapping, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        with self.assertRaises(KeepReview):
            resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers, secret=secret)

    def test_plan_isolation_with_different_numeric_ids_and_cross_match(self):
        """Instruction 1 & 3: Seed different numeric IDs for acquisition plans, trial plans, and batch plans.
        Include an unrelated anime whose trial plan ID equals another anime's acquisition plan ID.
        """
        # Anime A (aid=50, tid=100): user_evaluation_plan_id=10, acq_plan_id=30, res_id=20, verification_id=1, stage_job_id=14, op_id=20
        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001,
                                   user_evaluation_plan_id=10, acq_plan_id=30, res_id=20,
                                   verification_id=1, stage_job_id=14, op_id=20)

        # Unrelated Anime B (aid=60, tid=200): user_evaluation_plan_id=30 (matches Anime A's acq_plan_id!),
        # acq_plan_id=35, res_id=25, verification_id=2, stage_job_id=24, op_id=22
        self._seed_staged_unmapped(self.db, aid=60, tid=200, ep_num=1, eid=6001,
                                   user_evaluation_plan_id=30, acq_plan_id=35, res_id=25,
                                   verification_id=2, stage_job_id=24, op_id=22)

        # Batch Plan for Anime C (aid=70, tid=300): batch_plan_id=40, res_id=50, stage_job_id=15
        d64 = f"{70:064x}"
        c40 = f"{40:040x}"
        self.db.executescript(f"""
            INSERT INTO identified_anime VALUES(70, 300, 'Batch Show');
            INSERT INTO identity_results VALUES(70, 1, 70, 2, '{{"anime":{{"anidb_id":70,"tvdb_series_id":300}}}}', 1);
            INSERT INTO identified_episodes VALUES(7001, 70);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(701, 1, 'VERIFY_BATCH', '{{}}', 'DONE', 0, 0, 0);
            INSERT INTO batch_verifications(batch_id,job_id,identity_result_id,anidb_id,tvdb_series_id,tvdb_season,
              release_sha256,info_hash,info_sha256,verified_at_ms)
              VALUES(1, 701, 70, 70, 300, 1, '{d64}', '{c40}', '{d64}', 1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(702, 1, 'EVALUATE_BATCH', '{{}}', 'DONE', 0, 0, 0);
            INSERT INTO batch_user_evaluation_plans(plan_id,batch_id,evaluation_job_id,anchor_episode,
              backfill_count,following_count,configuration_revision,max_episode_bytes,max_plan_bytes,planned_size_bytes,created_at_ms)
              VALUES(40, 1, 702, 1, 5, 0, 1, 1000000, 5000000, 1000000, 1);
            INSERT INTO batch_user_evaluation_files(plan_id,file_index,tvdb_episode,selected_size_bytes)
              VALUES(40, 0, 1, 1000);
            INSERT INTO batch_verified_files(batch_id,file_index,file_path,file_size_bytes,release_episode,
              anidb_episode_number,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_episode_id)
              VALUES(1, 0, 's/S01E01.mkv', 1000, 1, 1, 7001, 1, 1, 9999);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(703, 1, 'RESERVE_BATCH', '{{}}', 'DONE', 0, 0, 0);
            INSERT INTO batch_episode_reservations(reservation_id,plan_id,owner_job_id,file_index,
              anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,state,reserved_at_ms)
              VALUES(50, 40, 703, 0, 70, 7001, 300, 1, 1, 9999, 'ACTIVE', 1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(15, 1, 'STAGE_BATCH_EPISODE', '{{}}', 'DONE', 0, 0, 0);
            INSERT INTO batch_staging_publications(plan_id,file_index,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms)
              VALUES(40, 0, 15, 701, '{{"entries":[{{"role":"media","path":"s/S01E01.mkv"}}],"root":"/staging"}}',
                     '[{{"role":"media","path":"s/S01E01.mkv"}}]', 'PUBLISHED', 1, 1);
        """)
        self.db.commit()

        # Both single trial plans have NULL placement in episode_release_selections
        sel_a = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=10").fetchone()
        sel_b = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=30").fetchone()
        self.assertEqual(sel_a, (None, None, None))
        self.assertEqual(sel_b, (None, None, None))

        # 1. Resolve TVDB placement for Anime A using target_job_id=14
        mapping_a = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" episodeoffset="0">'
                     b'</anime></anime-list>')
        providers_a = FakeProviders(mapping=mapping_a, tvdb=((9001, 1, 1),), airdates={1: '2026-10-01'})
        secret = Credentials('fake-key')

        res_a = resolve_tvdb_placement(self.db, 50, 5001, 100, 14, client=providers_a, secret=secret)
        self.assertEqual(res_a, (9001, 1, 1))

        # Check Anime A's selection (plan_id=10) IS updated
        sel_a_after = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=10").fetchone()
        self.assertEqual(sel_a_after, (1, 1, 9001))

        # Check Anime B's selection (plan_id=30, matching Anime A's acq_plan_id) is UNTOUCHED!
        sel_b_after = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=30").fetchone()
        self.assertEqual(sel_b_after, (None, None, None))

        # Check Anime A's reservation (res_id=20) IS updated
        res_a_row = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_reservations WHERE reservation_id=20").fetchone()
        self.assertEqual(res_a_row, (1, 1, 9001))

        # Check Anime B's reservation (res_id=25) is UNTOUCHED
        res_b_row = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_reservations WHERE reservation_id=25").fetchone()
        self.assertEqual(res_b_row, (None, None, None))

        # 2. Resolve TVDB placement for Anime C batch using target_job_id=15
        mapping_c = (b'<anime-list><anime anidbid="70" tvdbid="300" defaulttvdbseason="1" episodeoffset="0">'
                     b'</anime></anime-list>')
        providers_c = FakeProviders(mapping=mapping_c, tvdb=((9002, 1, 1),), airdates={1: '2026-10-01'},
                                    episodes={1: 7001})

        res_c = resolve_tvdb_placement(self.db, 70, 7001, 300, 15, client=providers_c, secret=secret)
        self.assertEqual(res_c, (9002, 1, 1))

        # Check Anime C's batch reservation (reservation_id=50, plan_id=40) IS updated
        batch_res_row = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM batch_episode_reservations WHERE reservation_id=50").fetchone()
        self.assertEqual(batch_res_row, (1, 1, 9002))

        # Confirm episode_release_selections was NOT touched for batch plan (neither plan 40 exists nor plan 30 changed)
        sel_b_still = self.db.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections WHERE plan_id=30").fetchone()
        self.assertEqual(sel_b_still, (None, None, None))
        sel_c_exists = self.db.execute("SELECT 1 FROM episode_release_selections WHERE plan_id=40").fetchone()
        self.assertIsNone(sel_c_exists)

    def test_missing_secret_during_keep_maps_to_keep_configuration_real_path(self):
        """Instruction 2: Missing TVDB credentials in real SecretStore map ProviderConfigurationError to KeepConfiguration."""
        import tempfile
        from src.nextgen.configuration import ConfigurationStore
        from src.nextgen.secrets import SecretStore
        from src.nextgen.sonarr import KeepConfiguration

        self._seed_staged_unmapped(self.db, aid=50, tid=100, ep_num=1, eid=5001)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_store = ConfigurationStore(tmpdir)
            cfg_store.initialize({"anidb": {"client_name": "testclient"}})
            sec_store = SecretStore(tmpdir)

            # Case A: Real uninitialized SecretStore without secrets file
            resolver = make_keep_resolver(cfg_store.read(), sec_store)
            with self.assertRaises(KeepConfiguration):
                resolver(self.db, 50, 5001, 100, 14)

            # Case B: Initialized SecretStore with empty tvdb_api_key
            sec_store.initialize(tvdb_api_key='')
            resolver2 = make_keep_resolver(cfg_store.read(), sec_store)
            with self.assertRaises(KeepConfiguration):
                resolver2(self.db, 50, 5001, 100, 14)


if __name__ == '__main__':
    unittest.main()
