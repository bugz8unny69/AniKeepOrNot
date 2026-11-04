"""Audio check setting: probe languages, Dual Audio ranking and the intake gate."""
import json
import unittest

from src.nextgen.acquisition.audio import audio_tagged, dual_audio_tagged, required_languages, wants_english
from src.nextgen.acquisition.media import AudioMissing, parse_probe
from src.nextgen.acquisition.prowlarr_search import DiscoveryPolicy, Release
from src.nextgen.jobs.evaluate_trial_job import (
    EvaluationCompletion, registration as trial_registration,
)
from src.nextgen.jobs.discover_sonarr_backfill_job import KIND as DISCOVER_KIND
from src.nextgen.storage.sonarr_backfill import window
from src.nextgen.storage.sqlite import (
    accept_event_and_job, connect, enqueue_job, save_identity, transaction,
)
from src.nextgen.tests import test_backfill_discovery as backfill
from src.nextgen.tests.test_media_verification import probe_bytes


class AudioTagTests(unittest.TestCase):
    def test_common_group_spellings_count_as_audio_tags(self):
        for title in ('[Group] Show - 01 (1080p) [Dual Audio]', 'Show.S01E01.1080p.Dual-Audio.x265',
                      'Show S01E01 DUAL 1080p', 'Show - 01 [Multi-Audio]', 'Show S01E01 MULTi 1080p',
                      'Show - 01 [English Dub]', 'Show - 01 (Dubbed)', 'Show S01E01 JPN+ENG',
                      'Show - 01 [Eng-Jpn]', 'Show - 01 (Japanese, English)',
                      'Show - 01 [MULTi AD]', '[SubsPlease] Some Show - 01 (720p) [MULTi AD].mkv'):
            with self.subTest(title=title):
                self.assertTrue(audio_tagged(title))

    def test_subtitles_and_plain_titles_do_not_count(self):
        for title in ('[SubsPlease] Show - 01 (1080p)', 'Show - 01 [Multi-Subs]',
                      'Show S01E01 1080p MULTi-Sub', 'Multiple Show - 01', '', None):
            with self.subTest(title=title):
                self.assertFalse(audio_tagged(title))

    def test_dual_audio_hint_requires_both_audio_languages(self):
        for title in ('Show - 01 [Dual Audio]', 'Show - 01 MULTi',
                      'Show - 01 [Multi-Audio]', 'Show - 01 [MULTi AD]',
                      '[SubsPlease] Some Show - 01 (720p) [MULTi AD].mkv',
                      'Show S01E01 JPN+ENG', 'Show (Japanese, English)'):
            with self.subTest(title=title):
                self.assertTrue(dual_audio_tagged(title))
        for title in ('Show - 01 [English Dub]', 'Show - 01 [Dubbed]',
                      'Show - 01 [Multi-Subs]', '[SubsPlease] Show - 01', ''):
            with self.subTest(title=title):
                self.assertFalse(dual_audio_tagged(title))


class ProbeLanguageTests(unittest.TestCase):
    def test_extra_audio_tracks_are_fine(self):
        raw = probe_bytes(languages=('fre', 'jpn', 'ger', 'eng', 'spa', 'ita'))
        self.assertEqual(parse_probe(raw, '.mkv')[1], ('eng', 'jpn'))

    def test_japanese_only_mode(self):
        self.assertEqual(parse_probe(probe_bytes(languages=('jpn',)), '.mkv', 'japanese')[1], ('jpn',))
        with self.assertRaises(AudioMissing):
            parse_probe(probe_bytes(languages=('eng',)), '.mkv', 'japanese')

    def test_both_required_by_default(self):
        with self.assertRaises(AudioMissing) as raised:
            parse_probe(probe_bytes(languages=('jpn',)), '.mkv')
        self.assertEqual(str(raised.exception), 'Japanese and English audio tracks are required.')

    def test_off_skips_the_language_check_only(self):
        self.assertEqual(parse_probe(probe_bytes(languages=('und',)), '.mkv', 'off')[1], ())
        self.assertEqual(required_languages('off'), frozenset())

    def test_default_and_empty_mode_defaults_to_japanese(self):
        self.assertEqual(required_languages(), frozenset({'jpn'}))
        self.assertEqual(required_languages(None), frozenset({'jpn'}))
        self.assertEqual(required_languages(''), frozenset({'jpn'}))
        self.assertFalse(wants_english(None))
        self.assertFalse(wants_english(''))

    def test_none_is_code_error(self):
        with self.assertRaises(ValueError) as ctx:
            required_languages('none')
        self.assertIn("'none' is not a valid language requirement", str(ctx.exception))
        with self.assertRaises(ValueError):
            wants_english('none')

    def test_configuration_audio_check_defaults_to_japanese_when_unset(self):
        from src.nextgen.configuration_schema import ConfigurationSnapshot, ConfigurationError, validate_values
        snap = ConfigurationSnapshot(1, {'acquisition': {'audio_check': ''}})
        self.assertEqual(snap.value('acquisition.audio_check'), 'japanese')
        with self.assertRaises(ConfigurationError):
            validate_values({'acquisition': {'audio_check': 'none'}})

    def test_multi_ad_hint_still_requires_actual_dual_audio_in_probe(self):
        title = '[SubsPlease] Some Show - 01 (720p) [MULTi AD].mkv'
        self.assertTrue(dual_audio_tagged(title))

        # 1. Probe contains both jpn and eng -> passes
        both = probe_bytes(languages=('jpn', 'eng'))
        self.assertEqual(parse_probe(both, '.mkv')[1], ('eng', 'jpn'))

        # 2. Probe contains only jpn -> fails
        jpn_only = probe_bytes(languages=('jpn',))
        with self.assertRaises(AudioMissing):
            parse_probe(jpn_only, '.mkv')

        # 3. Probe contains only eng -> fails
        eng_only = probe_bytes(languages=('eng',))
        with self.assertRaises(AudioMissing):
            parse_probe(eng_only, '.mkv')

        # 4. Probe contains French / other language only -> fails
        fre_only = probe_bytes(languages=('fre',))
        with self.assertRaises(AudioMissing):
            parse_probe(fre_only, '.mkv')


def release(title, resolution=1080, seeders=50):
    return Release(title, 'nyaa', resolution, seeders, 100, 1, None, eligible_for_handoff=True)


class AudioRankingTests(unittest.TestCase):
    def test_english_required_excludes_untagged_and_dub_only_candidates(self):
        plain = release('[A] Show - 01 (1080p)', seeders=500)
        dual = release('[B] Show - 01 (720p) [Dual Audio]', resolution=720, seeders=10)
        ranked = DiscoveryPolicy(prefer_audio_tag=True).rank((plain, dual))
        self.assertEqual(ranked, (dual, plain))
        self.assertTrue(DiscoveryPolicy(prefer_audio_tag=True).accepts(dual))
        self.assertFalse(DiscoveryPolicy(prefer_audio_tag=True).accepts(plain))
        self.assertFalse(DiscoveryPolicy(prefer_audio_tag=True).accepts(release('Show - 01 [English Dub]')))
        self.assertEqual(DiscoveryPolicy().rank((plain, dual)), (plain, dual))


class AnchorWindowTests(unittest.TestCase):
    def test_include_anchor_adds_the_delivered_episode(self):
        self.assertEqual(window(7, 6, 1), (1, 2, 3, 4, 5, 6))
        self.assertEqual(window(7, 6, 1, True), (1, 2, 3, 4, 5, 6, 7))
        # AniDB window: a later anchor backfills the episodes just before it.
        self.assertEqual(window(10, 6, 1), (4, 5, 6, 7, 8, 9))

    def test_untagged_delivery_does_not_route_to_reservation(self):
        spec = trial_registration(reservation_kind='RESERVE_EPISODE', discovery_kind=DISCOVER_KIND)
        skipped = EvaluationCompletion(None, 1, True)
        kept = EvaluationCompletion(None, 1, False)
        self.assertEqual(spec.route(skipped), ())
        self.assertEqual(len(spec.route(kept)), 1)


class IntakeAudioSearchTests(unittest.IsolatedAsyncioTestCase):
    """An untagged autobrr release is not downloaded; Prowlarr searches its episode."""
    asyncSetUp = backfill.BackfillWorkerTests.asyncSetUp
    rows = backfill.BackfillWorkerTests.rows
    worker = backfill.BackfillWorkerTests.worker

    def autobrr(self, episode, title):
        db = connect(self.database)
        try:
            body = json.dumps({'release_name': title}).encode()
            event, release_job = accept_event_and_job(db, 'autobrr', body, kind='VERIFY_RELEASE',
                                                      payload={'anidb_id': 123})
            identity = save_identity(db, event, {'schema_version': 2, 'anime': {
                'anidb_id': 123, 'tvdb_series_id': 456, 'title': 'Test'}})
            eid = 11000 + episode
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?", (release_job,))
                db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?,123)', (eid,))
                db.execute(
                    "INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,"
                    "verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,verified_at_ms) "
                    "VALUES(?,?,123,?,'DECLARED_SINGLE_EPISODE',?,?,456,1,?,?,1)",
                    (release_job, identity, 'a'*64, episode, eid, episode, eid+100000))
                enqueue_job(db, kind='EVALUATE_TRIAL', payload={'verification_job_id': release_job},
                            component='worker', event_id=event, created_by_job_id=release_job,
                            available_at_ms=0)
        finally:
            db.close()

    async def run_pages(self, title, *, audio=None):
        if audio is not None:
            self.store.update({'acquisition': {'audio_check': audio}}, expected_revision=1)
        self.autobrr(7, title)
        fake = backfill.FakeDiscovery()
        worker = self.worker(fake)
        for _ in range(3):
            await worker.attempt()
        marked = self.rows("SELECT reason_code FROM job_history WHERE event_code='TRIAL_AUDIO_SEARCH'")
        return [call[1] for call in fake.calls], marked

    async def test_untagged_release_searches_its_own_episode(self):
        calls, marked = await self.run_pages('[SubsPlease] Test - 07 (1080p)')
        self.assertEqual(calls, [(1, 2, 3, 4), (5, 6, 7)])
        self.assertEqual(marked, [('RELEASE_AUDIO_UNTAGGED',)])

    async def test_dual_audio_release_is_downloaded_as_before(self):
        calls, marked = await self.run_pages('[Group] Test - 07 (1080p) [Dual Audio]')
        self.assertEqual(calls, [(1, 2, 3, 4), (5, 6)])
        self.assertEqual(marked, [])

    async def test_english_only_autobrr_release_falls_back_to_prowlarr(self):
        calls, marked = await self.run_pages('[Group] Test - 07 (1080p) [English Dub]')
        self.assertEqual(calls, [(1, 2, 3, 4), (5, 6, 7)])
        self.assertEqual(marked, [('RELEASE_AUDIO_UNTAGGED',)])

    async def test_japanese_only_mode_never_skips_the_delivery(self):
        calls, marked = await self.run_pages('[SubsPlease] Test - 07 (1080p)', audio='japanese')
        self.assertEqual(calls, [(1, 2, 3, 4), (5, 6)])
        self.assertEqual(marked, [])



if __name__ == '__main__':
    unittest.main()
