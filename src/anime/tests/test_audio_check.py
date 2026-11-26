"""Audio check setting: probe languages, Dual Audio ranking and the intake gate."""
import json
import unittest

from src.anime.acquisition.audio import dual_audio_tagged, required_languages, wants_english
from src.anime.acquisition.media import AudioMissing, parse_probe
from src.anime.acquisition.prowlarr_search import DiscoveryPolicy, Release
from src.anime.tests.test_media_verification import probe_bytes


class AudioTagTests(unittest.TestCase):
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
        from src.anime.configuration_schema import ConfigurationSnapshot, ConfigurationError, validate_values
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







if __name__ == '__main__':
    unittest.main()
