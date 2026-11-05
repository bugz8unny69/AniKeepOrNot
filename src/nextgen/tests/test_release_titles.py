"""How release groups name single episodes, and which ones match a target episode."""
import unittest

from src.nextgen.acquisition.prowlarr_search import EpisodeTarget, declared_title
from src.nextgen.acquisition.release_titles import (
    AnimeTitles, matches, parse_release, search_queries, season_marker,
)
from src.nextgen.providers.release_mapping import declared_episode

SEASON_ONE = ('Kusuriya no Hitorigoto', 'The Apothecary Diaries', '薬屋のひとりごと')
SEQUEL = ('Kusuriya no Hitorigoto (2025)', 'Kusuriya no Hitorigoto 2nd Season',
          'The Apothecary Diaries Season 2', '薬屋のひとりごと 第2期')

# (anime titles, TVDB season, (TVDB episode, AniDB episode, absolute), release, matches?)
CASES = (
    # Season 1, episode 5: the usual group styles.
    (SEASON_ONE, 1, (5, 5, 5), '[SubsPlease] Kusuriya no Hitorigoto - 05 (1080p) [ABCD1234].mkv', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Erai-raws] Kusuriya no Hitorigoto - 05 [1080p][Multiple Subtitle][ABCD]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Judas] Kusuriya no Hitorigoto - S01E05 [1080p][HEVC x265 10bit][Multi-Subs]', True),
    (SEASON_ONE, 1, (5, 5, 5), 'The.Apothecary.Diaries.S01E05.1080p.NF.WEB-DL.DDP2.0.H.264-VARYG', True),
    (SEASON_ONE, 1, (5, 5, 5), 'Kusuriya no Hitorigoto S01E05 1080p CR WEB-DL AAC2.0 H 264-VARYG', True),
    (SEASON_ONE, 1, (5, 5, 5), '[ASW] Kusuriya no Hitorigoto - 05 [1080p HEVC x265 10Bit][AAC]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto | The Apothecary Diaries - 05 (1080p)', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto (The Apothecary Diaries) - 05 [1080p]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[EMBER] Kusuriya no Hitorigoto - 05v2 [1080p]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto - Episode 05 [1080p]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto Ep 05 [1080p]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto 05 [1080p]', True),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya_no_Hitorigoto_-_05_[1080p].mkv', True),
    # Not this episode, or not one episode.
    (SEASON_ONE, 1, (5, 5, 5), '[SubsPlease] Kusuriya no Hitorigoto - 06 (1080p)', False),
    (SEASON_ONE, 1, (5, 5, 5), '[SubsPlease] Kusuriya no Hitorigoto (01-24) (1080p) [Batch]', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto - 01~12 [1080p]', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto - 05-06 [1080p]', False),
    (SEASON_ONE, 1, (5, 5, 5), 'Kusuriya no Hitorigoto S01E05E06 1080p WEB', False),
    (SEASON_ONE, 1, (5, 5, 5), 'Kusuriya no Hitorigoto S01E05-E06 1080p WEB', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto - 05.5 [1080p]', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto - 05 [OVA]', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto S2 - 05 (1080p)', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto S02E05 (1080p)', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto Movie - 05 (1080p)', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Other Show - 05 (1080p)', False),
    (SEASON_ONE, 1, (5, 5, 5), '[Group] Kusuriya no Hitorigoto Gaiden - 05 (1080p)', False),
    # Sequel whose TVDB entry is season 2 (season 1 had 24 episodes).
    (SEQUEL, 2, (5, 5, 29), '[SubsPlease] Kusuriya no Hitorigoto - 29 (1080p)', True),
    (SEQUEL, 2, (5, 5, 29), '[Erai-raws] Kusuriya no Hitorigoto 2nd Season - 05 [1080p CR WEB-DL AVC AAC][MultiSub]', True),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto S2 - 05 (1080p)', True),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto Season 2 - 05 (1080p)', True),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto Second Season - 05 (1080p)', True),
    (SEQUEL, 2, (5, 5, 29), 'Kusuriya no Hitorigoto S02E05 1080p CR WEB-DL AAC2.0 H 264-VARYG', True),
    (SEQUEL, 2, (5, 5, 29), 'The Apothecary Diaries S02E05 1080p WEB', True),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto (2025) - 05 [1080p]', True),
    (SEQUEL, 2, (5, 5, 29), '[SubsPlease] Kusuriya no Hitorigoto - 05 (1080p)', False),  # Season 1 episode 5.
    (SEQUEL, 2, (5, 5, 29), '[Judas] Kusuriya no Hitorigoto - S01E05 [1080p]', False),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto S3 - 05 (1080p)', False),
    (SEQUEL, 2, (5, 5, 29), '[Group] Kusuriya no Hitorigoto Season 2 - 06 (1080p)', False),
    # Same sequel where TVDB keeps one continuous season (episode 29).
    (SEQUEL, 1, (29, 5, 29), '[SubsPlease] Kusuriya no Hitorigoto - 29 (1080p)', True),
    (SEQUEL, 1, (29, 5, 29), '[Group] Kusuriya no Hitorigoto S2 - 05 (1080p)', True),
    (SEQUEL, 1, (29, 5, 29), 'Kusuriya no Hitorigoto S02E05 1080p WEB', True),
    (SEQUEL, 1, (29, 5, 29), 'Kusuriya no Hitorigoto S01E29 1080p WEB', True),
    (SEQUEL, 1, (29, 5, 29), '[SubsPlease] Kusuriya no Hitorigoto - 05 (1080p)', False),
    # Titles that end in numbers or carry their own season numbering.
    (('Mob Psycho 100',), 1, (5, 5, 5), '[Group] Mob Psycho 100 - 05 [1080p]', True),
    (('Mob Psycho 100',), 1, (5, 5, 5), '[Group] Mob Psycho 100 [1080p]', False),
    (('Mob Psycho 100 III', 'Mob Psycho 100 Season 3'), 3, (5, 5, 29),
     '[Group] Mob Psycho 100 III - 05 [1080p]', True),
    (('Mob Psycho 100 III', 'Mob Psycho 100 Season 3'), 3, (5, 5, 29),
     '[Group] Mob Psycho 100 - 05 [1080p]', False),
)


class ReleaseTitleTests(unittest.TestCase):
    def test_group_naming_styles(self):
        for titles, season, (n, a, absolute), release, expected in CASES:
            target = EpisodeTarget(n, 'SEARCHABLE', a, 9000 + n, absolute)
            with self.subTest(release=release, season=season):
                self.assertEqual(matches(parse_release(release), AnimeTitles.build(titles, season),
                                         target, season), expected)

    def test_season_markers(self):
        self.assertEqual(season_marker('Title 2nd Season'), ('Title', 2))
        self.assertEqual(season_marker('Title Season 3'), ('Title', 3))
        self.assertEqual(season_marker('Title S4'), ('Title', 4))
        self.assertEqual(season_marker('Title II'), ('Title', 2))
        self.assertEqual(season_marker('Title (2025)'), ('Title (2025)', None))

    def test_year_qualified_anime_matches_explicit_season_without_accepting_wrong_season(self):
        titles = ('Koori no Jouheki (2026)', 'The Ramparts of Ice (2026)')
        target = EpisodeTarget(2, 'SEARCHABLE', 2, 9002, 14)
        prepared = AnimeTitles.build(titles, 2)
        self.assertTrue(matches(parse_release('The Ramparts of Ice S02E02 1080p WEB'),
                                prepared, target, 2))
        self.assertTrue(matches(parse_release('[Group] Koori no Jouheki S02E02 (1080p)'),
                                prepared, target, 2))
        self.assertFalse(matches(parse_release('The Ramparts of Ice S01E02 1080p WEB'),
                                 prepared, target, 2))
        self.assertFalse(matches(parse_release('The Ramparts of Ice - 02 [1080p]'),
                                 prepared, target, 2))
        self.assertFalse(matches(parse_release('Other Anime S02E02 1080p WEB'),
                                 prepared, target, 2))

    def test_yearless_queries_cover_the_official_season_episode(self):
        titles = ('Koori no Jouheki (2026)', 'The Ramparts of Ice (2026)')
        target = EpisodeTarget(2, 'SEARCHABLE', 2, 9002, 14)
        queries = search_queries(titles, AnimeTitles.build(titles, 2), target)
        self.assertIn('Koori no Jouheki 02', queries)
        self.assertIn('The Ramparts of Ice 02', queries)
        self.assertIn('The Ramparts of Ice S02E02', queries)
        self.assertTrue(all('(2026)' not in q for q in queries))
        self.assertLessEqual(len(queries), 6)

    def test_verification_title_is_always_readable(self):
        for titles in (SEASON_ONE, SEQUEL, ('Mob Psycho 100',), ('Re:Zero kara Hajimeru Isekai Seikatsu',)):
            with self.subTest(titles=titles):
                self.assertEqual(declared_episode(declared_title(titles, 7)), ('anidb', None, 7))

    def test_queries_reach_absolute_and_season_styles(self):
        target = EpisodeTarget(5, 'SEARCHABLE', 5, 9005, 29)
        queries = search_queries(SEQUEL, AnimeTitles.build(SEQUEL, 2), target)
        self.assertIn('Kusuriya no Hitorigoto 05', queries)
        self.assertIn('Kusuriya no Hitorigoto 29', queries)
        self.assertIn('Kusuriya no Hitorigoto S02E05', queries)
        self.assertLessEqual(len(queries), 6)

    def test_season_one_queries_generate_s01e01_traditional_and_select_english(self):
        titles = (
            'Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de, Ikkodate Mezashite Youhei to Shite Jiyuu ni Ikitai',
            'Saikyosoubi',
            'Reborn as a Space Mercenary',
        )
        target = EpisodeTarget(1, 'SEARCHABLE', 1, 1001, 1)
        prepared = AnimeTitles.build(titles, 1)

        # 1. Season 1 without audio requirement
        queries = search_queries(titles, prepared, target, season=1, english_title='Reborn as a Space Mercenary')
        self.assertIn('Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de, Ikkodate Mezashite Youhei to Shite Jiyuu ni Ikitai 01', queries)
        self.assertIn('Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de, Ikkodate Mezashite Youhei to Shite Jiyuu ni Ikitai S01E01', queries)
        self.assertIn('Reborn as a Space Mercenary 01', queries)
        self.assertIn('Reborn as a Space Mercenary S01E01', queries)
        self.assertIn('Saikyosoubi 01', queries)
        self.assertLessEqual(len(queries), 6)

        # 2. Season 1 with audio requirement (prefer_audio=True)
        audio_queries = search_queries(titles, prepared, target, season=1,
                                       english_title='Reborn as a Space Mercenary', prefer_audio=True)
        self.assertEqual(audio_queries[0], 'Reborn as a Space Mercenary S01E01')
        self.assertEqual(audio_queries[1], 'Reborn as a Space Mercenary S01E01 Dual-Audio')
        self.assertIn('Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de, Ikkodate Mezashite Youhei to Shite Jiyuu ni Ikitai 01', audio_queries)
        self.assertIn('Mezametara Saikyou Soubi to Uchuusen Mochi Datta no de, Ikkodate Mezashite Youhei to Shite Jiyuu ni Ikitai S01E01', audio_queries)
        self.assertIn('Reborn as a Space Mercenary 01', audio_queries)
        self.assertIn('Saikyosoubi 01', audio_queries)
        self.assertLessEqual(len(audio_queries), 6)


    def test_audio_first_sequel_reserves_absolute_episode_in_six_query_budget(self):
        # A separately indexed AniDB sequel maps to TVDB S02E05; some
        # groups publish the same episode as absolute episode 29.
        target = EpisodeTarget(5, 'SEARCHABLE', 5, 9005, 29)
        queries = search_queries(SEQUEL, AnimeTitles.build(SEQUEL, 2), target,
                                 season=2, english_title='The Apothecary Diaries Season 2',
                                 prefer_audio=True)
        self.assertEqual(len(queries), 6)
        self.assertEqual(queries[:2], ('The Apothecary Diaries S02E05',
                                       'The Apothecary Diaries S02E05 Dual-Audio'))
        self.assertIn('Kusuriya no Hitorigoto 05', queries)
        self.assertIn('Kusuriya no Hitorigoto S02E05', queries)
        self.assertIn('Kusuriya no Hitorigoto 29', queries)

    def test_year_qualified_sequel_without_season_marker_searches_absolute_number(self):
        # An AniDB sequel may use year-qualified catalog titles but no
        # season marker. The verified TVDB context still supplies the number.
        titles = ('Koori no Jouheki (2026)', 'The Ramparts of Ice (2026)')
        target = EpisodeTarget(2, 'SEARCHABLE', 2, 9002, 14)
        prepared = AnimeTitles.build(titles, 2)
        queries = search_queries(titles, prepared, target, season=2,
                                 english_title='The Ramparts of Ice (2026)',
                                 prefer_audio=True)
        self.assertIn('The Ramparts of Ice S02E02', queries)
        self.assertIn('Koori no Jouheki 02', queries)
        self.assertIn('Koori no Jouheki 14', queries)
        self.assertLessEqual(len(queries), 6)
        self.assertTrue(matches(parse_release('[Group] Koori no Jouheki - 14 (1080p)'),
                                prepared, target, 2))
        self.assertFalse(matches(parse_release('[Group] Koori no Jouheki - 13 (1080p)'),
                                 prepared, target, 2))

    def test_sequel_without_english_alias_keeps_absolute_and_seasonless_formats(self):
        titles = ('Aoi no Sekai 2nd Season', 'Aoi no Sekai', 'Ao no Sekai')
        target = EpisodeTarget(3, 'SEARCHABLE', 1, 9003, 27)  # mapping offset: AniDB 1 -> S02E03
        queries = search_queries(titles, AnimeTitles.build(titles, 2), target,
                                 season=2, prefer_audio=True)
        self.assertEqual(queries[:2], ('Aoi no Sekai 2nd Season 01',
                                       'Aoi no Sekai S02E03'))
        self.assertIn('Aoi no Sekai 27', queries)
        self.assertIn('Aoi no Sekai 01', queries)
        self.assertLessEqual(len(queries), 6)

    def test_romanized_aliases_without_anidb_english_do_not_get_english_priority(self):
        titles = ('Hoshizora no Shita', 'Yoru no Hoshizora')
        target = EpisodeTarget(5, 'SEARCHABLE', 5, 9005, 5)
        prepared = AnimeTitles.build(titles, 1)
        for prefer_audio in (False, True):
            with self.subTest(prefer_audio=prefer_audio):
                queries = search_queries(titles, prepared, target, season=1, prefer_audio=prefer_audio)
                self.assertEqual(queries[:2], ('Hoshizora no Shita 05', 'Hoshizora no Shita S01E05'))
                self.assertIn('Yoru no Hoshizora 05', queries)
                self.assertNotIn('Yoru no Hoshizora S01E05 Dual-Audio', queries)
                self.assertLessEqual(len(queries), 6)

    def test_anidb_english_title_gets_priority_even_when_not_second(self):
        titles = ('Kusuriya no Hitorigoto', 'Kusuriya', 'The Apothecary Diaries')
        target = EpisodeTarget(5, 'SEARCHABLE', 5, 9005, 5)
        queries = search_queries(titles, AnimeTitles.build(titles, 1), target, season=1,
                                 english_title='The Apothecary Diaries', prefer_audio=True)
        self.assertEqual(queries[:2], ('The Apothecary Diaries S01E05',
                                       'The Apothecary Diaries S01E05 Dual-Audio'))

    def test_pipe_delimited_and_season_releases_map_only_to_target_episode(self):
        titles = ('Kusuriya no Hitorigoto', 'The Apothecary Diaries')
        target = EpisodeTarget(5, 'SEARCHABLE', 5, 9005, 5)
        prepared = AnimeTitles.build(titles, 1)

        # Pipe-delimited releases match
        self.assertTrue(matches(parse_release('[Group] Kusuriya no Hitorigoto | The Apothecary Diaries - 05 (1080p)'),
                                prepared, target, 1))
        self.assertTrue(matches(parse_release('[Group] The Apothecary Diaries | Kusuriya no Hitorigoto S01E05 1080p'),
                                prepared, target, 1))

        # Different episodes or unsupported types do not match
        self.assertFalse(matches(parse_release('[Group] Kusuriya no Hitorigoto | The Apothecary Diaries - 06 (1080p)'),
                                 prepared, target, 1))
        self.assertFalse(matches(parse_release('[Group] Kusuriya no Hitorigoto | The Apothecary Diaries - 01-12 Batch (1080p)'),
                                 prepared, target, 1))
        self.assertFalse(matches(parse_release('[Group] Kusuriya no Hitorigoto Recap - 05 (1080p)'),
                                 prepared, target, 1))


if __name__ == '__main__':
    unittest.main()


class IntakeSharesTheReaderTests(unittest.TestCase):
    """Webhook intake reads the same group styles as backfill search."""

    def test_declared_episode_for_group_styles(self):
        from src.nextgen.release_contracts import ReleaseNotReady
        cases = {
            '[SubsPlease] Kusuriya no Hitorigoto - 05 (1080p) [ABCD1234].mkv': ('anidb', None, 5),
            'The.Apothecary.Diaries.S01E05.1080p.NF.WEB-DL.DDP2.0.H.264-VARYG': ('tvdb', 1, 5),
            '[Group] Kusuriya no Hitorigoto 05 [1080p]': ('anidb', None, 5),
            '[Group] Kusuriya_no_Hitorigoto_-_05_[1080p].mkv': ('anidb', None, 5),
            '[Group] Kusuriya no Hitorigoto S2 - 05 (1080p)': ('anidb', None, 5),
            '[Group] Kusuriya no Hitorigoto Season 2 - 05 (1080p)': ('anidb', None, 5),
            '[Erai-raws] Kusuriya no Hitorigoto 2nd Season - 05 [1080p]': ('anidb', None, 5),
            '[Group] Mob Psycho 100 - 05 [1080p]': ('anidb', None, 5),
            '[Group] Kusuriya no Hitorigoto | The Apothecary Diaries - 05 (1080p)': ('anidb', None, 5),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(declared_episode(title), expected)
        for title in ('[Group] Mob Psycho 100 [1080p]', 'One Piece 1100 [1080p]',
                      '[Group] Title S2 - 05 | Title Season 3'):
            with self.subTest(title=title), self.assertRaises(ReleaseNotReady):
                declared_episode(title)

    def test_identity_aliases_and_seasons(self):
        from src.nextgen.providers.identity import _release_seasons
        from src.nextgen.providers.parsing import release_title
        aliases_2nd, ep_2nd = release_title('[Erai-raws] Kusuriya no Hitorigoto 2nd Season - 05 [1080p]')
        self.assertEqual(ep_2nd, 5)
        self.assertIn('Kusuriya no Hitorigoto 2nd Season', aliases_2nd)
        self.assertIn('Kusuriya no Hitorigoto', aliases_2nd)
        aliases_plain, ep_plain = release_title('[Group] Kusuriya no Hitorigoto 05 [1080p]')
        self.assertEqual((aliases_plain, ep_plain), (['Kusuriya no Hitorigoto'], 5))
        self.assertEqual(release_title('[Group] Mob Psycho 100 III - 05 [1080p]')[0], ['Mob Psycho 100 III'])
        # Operator issue fixtures:
        # 1. Steamboy with unclosed (BD Remux packaging tag
        self.assertEqual(release_title('Steamboy (2004) (BD Remux')[0], ['Steamboy (2004)', 'Steamboy'])
        # 2. Packaging metadata tag should not leak as an anime title alias
        mt_aliases, _ = release_title('[Group] Mushoku Tensei III: Special (Dual-Audio, Multi-Subs, Batch)')
        self.assertNotIn('Dual-Audio, Multi-Subs, Batch', mt_aliases)
        self.assertIn('Mushoku Tensei III: Special', mt_aliases)
        # 3. Unbalanced opening parenthesis stripped cleanly
        self.assertEqual(release_title('Jujutsu Kaisen ( 01 [1080p]')[0], ['Jujutsu Kaisen'])
        # 4. Opaque internal series ID not used as human-readable title
        self.assertEqual(release_title('T00393120 S01E01')[0], [])
        # "Season - 05" is never read as season 5.
        self.assertEqual(_release_seasons('[Erai-raws] Kusuriya no Hitorigoto 2nd Season - 05 [1080p]'), ['2'])
        self.assertEqual(_release_seasons('[Group] Title Second Season - 05'), ['2'])
        self.assertEqual(_release_seasons('[Group] Title - 05'), [])
        self.assertEqual(_release_seasons(
            '[Fuechslein] KILL la KILL - S00E01 (BD 1080p AV1 Opus 2.0) '
            '[Dual-Audio] | Goodbye Once Again (S01E25) (OVA)'), ['0'])

    def test_single_episodes_are_not_routed_as_packs(self):
        from src.nextgen.batch.recognition import release_scope_hint
        for title, season in (('[Group] Kusuriya no Hitorigoto S2 - 05 (1080p)', 2),
                              ('[Erai-raws] Kusuriya no Hitorigoto 2nd Season - 05 [1080p]', 2),
                              ('[Group] Mob Psycho 100 - 05 [1080p]', None)):
            with self.subTest(title=title):
                hint = release_scope_hint(title)
                self.assertEqual((hint.kind, hint.season_hint, hint.episode_hint),
                                 ('single_episode', season, 5))
        self.assertEqual(release_scope_hint('[Group] Kusuriya no Hitorigoto - 01~12 [1080p]').kind, 'ambiguous')
        self.assertEqual(release_scope_hint('[Trix] Cyberpunk Edgerunners S02 (Season S2) (Batch)').kind,
                         'season_pack')
        self.assertEqual(release_scope_hint(
            '[ToonsHub] A Returners Magic Should Be Special S02E04 1080p CR WEB-DL'
        ).kind, 'single_episode')
        self.assertEqual(release_scope_hint(
            '[ToonsHub] My Happy Marriage S00E02 Special Part 1'
        ).kind, 'special')
