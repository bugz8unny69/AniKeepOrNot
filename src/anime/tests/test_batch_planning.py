"""Offline tests of advisory batch classification and pure selection planning."""
import unittest

from src.anime.acquisition.metadata import BatchInventory, TorrentFile
from src.anime.batch.recognition import release_scope_hint
from src.anime.batch.selection import BatchPlanningError, plan_verified_batch


def inventory(*paths):
    return BatchInventory('a' * 40, 'b' * 64, tuple(TorrentFile(i, p, 100) for i, p in enumerate(paths)))


class BatchHintsTests(unittest.TestCase):
    def test_shiki_mixed_regular_season_and_ova(self):
        hint = release_scope_hint('[Sristi] Shiki (S01 + OVA) [BD Remux 1080p]')
        self.assertEqual((hint.kind, hint.season_hint, hint.episode_hint), ('mixed_pack', 1, None))

    def test_season_two_batch_and_single_episode(self):
        self.assertEqual(release_scope_hint('[Trix] Cyberpunk Edgerunners S02 (Season S2) (Batch)').kind,
                         'season_pack')
        self.assertEqual(release_scope_hint('Show S02E03').kind, 'single_episode')
        self.assertEqual(release_scope_hint('Show S02E03').season_hint, 2)
        self.assertEqual(release_scope_hint('Show S02E03').episode_hint, 3)

    def test_ambiguous_and_special_titles_are_not_regular_episodes(self):
        for title in ('Show S01E01 + S02E01', 'Show S01E01E02', 'Show S01E02-01'):
            with self.subTest(title=title):
                self.assertEqual(release_scope_hint(title).kind, 'ambiguous')
        # Season-scoped ranges route to batch verification (6eec137); the
        # hash-verified per-file inventory decides the actual coverage.
        hint=release_scope_hint('Show S01E01-02')
        self.assertEqual((hint.kind,hint.season_hint,hint.episode_hint),('season_pack',1,1))
        self.assertEqual(release_scope_hint('Show S00E05 OVA').kind, 'special')
        self.assertEqual(release_scope_hint('Shiki OVA').kind, 'special')


class ManifestSelectionTests(unittest.TestCase):
    def test_shiki_season_with_ova_and_sidecars(self):
        names = tuple(f'Shiki/Season 01/Shiki S01E{i:02d}.mkv' for i in range(1, 13)) + (
            'Shiki/OVA/Shiki OVA 01.mkv', 'Shiki/Season 01/info.nfo')
        plan = plan_verified_batch(inventory(*names), season=1,
                                   episode_map={i: i for i in range(1, 13)})
        self.assertEqual(plan.probe_index, 0)
        self.assertEqual(plan.episode_indices, tuple((i, i-1) for i in range(1, 7)))
        self.assertEqual(plan.remaining_indices, (1, 2, 3, 4, 5))
        self.assertEqual(plan.extra_indices, (12,))
        self.assertEqual(plan.skipped_indices, tuple(range(6, 14)))

    def test_absolute_episode_mapping_requires_explicit_map(self):
        files = inventory(*[f'Show/Season 02/Show - {i:02d}.mkv' for i in range(13, 25)])
        with self.assertRaises(BatchPlanningError):
            plan_verified_batch(files, season=2, episode_map={i: i for i in range(1, 13)})
        plan = plan_verified_batch(files, season=2,
                                   episode_map={i: i-12 for i in range(13, 25)})
        self.assertEqual(plan.episode_indices, tuple((i, i-1) for i in range(1, 7)))

    def test_unsafe_ambiguity_stops_entire_plan(self):
        cases = (
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 01/Show S01E01 alt.mkv'),
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 02/Show S02E02.mkv'),
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 01/Show S01E01E02.mkv'),
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 01/Unknown.mkv'),
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 01/Show - 2026.mkv'),
            ('Show/Season 01/Show S01E01.mkv', 'Show/Season 01/Show S01E02 OVA.mkv'),
        )
        for names in cases:
            with self.subTest(names=names), self.assertRaises(BatchPlanningError):
                plan_verified_batch(inventory(*names), season=1, episode_map={1: 1, 2: 2})

    def test_no_first_episode_or_bogus_mapping_never_produces_plan(self):
        with self.assertRaises(BatchPlanningError):
            plan_verified_batch(inventory('Show/Season 01/Show E03.mkv'), season=1,
                                episode_map={3: 3})
        with self.assertRaises(ValueError):
            plan_verified_batch(inventory('Show/Season 01/Show E01.mkv'), season=1,
                                episode_map={1: 1, 2: 1})



class ReleaseFileNameTests(unittest.TestCase):
    """Real season packs that were wrongly stopped (production jobs in brackets)."""
    def episode(self, name, season):
        from src.anime.batch.selection import file_episode_number
        return file_episode_number('Pack/' + name, season)

    def test_crc_checksum_is_not_an_episode(self):   # 664, 888, 832
        self.assertEqual(self.episode('[whomst-mini] Cyberpunk Edgerunners - S02E08 [WEB 1080p AV1 Opus] [Dual Audio] [E02FF655].mkv', 2), 8)
        self.assertEqual(self.episode('[whomst-mega] Cyberpunk Edgerunners - S02E03 [WEB 2160p x265 EAC3] [Dual Audio] [4E57E8F9].mkv', 2), 3)
        self.assertEqual(self.episode('[GetItTwisted] Fire Force - S02E21 [BD 1080p AVC Opus Dual-Audio] [E4B21E72].mkv', 2), 21)

    def test_season_tag_before_dash_number_is_not_a_range(self):   # 845, 796
        self.assertEqual(self.episode('[Raze] Cyberpunk Edgerunners S2 - 10 (Dual-Audio) (Multi-Sub) DDP5.1 x265 10bit 1080p 143.8561fps.mkv', 2), 10)
        self.assertEqual(self.episode('[Salieri] Spy x Family - S3 - 12 (1080p) (HDR) [Dual Audio].mkv', 3), 12)

    def test_version_suffix(self):   # 1304, 894, 832, 751
        self.assertEqual(self.episode('[Judas] Seihantai - S02E01v2.mkv', 2), 1)
        self.assertEqual(self.episode('[Judas] Re.Zero - S04E19v2.mkv', 4), 19)
        self.assertEqual(self.episode('[FoundYears] That Time I Got Reincarnated as a Slime - S04E01 v2 (CR WEB-DL 1080p AVC EAC-3) [Dual-Audio].mkv', 4), 1)

    def test_underscore_dash_and_ordinal_season(self):   # 985
        name = '[DB]ReZero kara Hajimeru Isekai Seikatsu 4th Season_-_07_(Dual Audio_10bit_1080p_x265).mkv'
        self.assertEqual(self.episode(name, 4), 7)
        with self.assertRaises(BatchPlanningError):
            self.episode(name, 3)

    def test_episode_title_after_tag_is_ignored(self):   # 782
        self.assertEqual(self.episode('Cyberpunk Edgerunners - S02E03 - 0 + 2 = 1.mkv', 2), 3)

    def test_half_episode_is_an_extra(self):   # 948
        self.assertIsNone(self.episode('[Sristi] Shiki - 20.5.mkv', 1))
        self.assertEqual(self.episode('[Sristi] Shiki - 20.mkv', 1), 20)

    def test_real_multi_episode_and_conflicts_still_stop(self):
        for name in ('Show S01E01-E02.mkv', 'Show S01E01-02.mkv', 'Show S01E01 & E02.mkv',
                     'Show - 01-02.mkv', 'Show - 13 (S01E01).mkv'):
            with self.subTest(name=name), self.assertRaises(BatchPlanningError):
                self.episode(name, 1)

    def test_disc_extras_are_set_aside(self):   # Call of the Night: RUBaDUB, Anime Time, TTGA
        for name in ('[RUBaDUB][1080p] Call of the Night - S01 Clean Closing [BD x265 10bit AC3][416D9C8E].mkv',
                     '[RUBaDUB][1080p] Call of the Night - S01 Clean Opening [BD x265 10bit AC3][751CB56D].mkv',
                     '[RUBaDUB][1080p] Call of the Night - S01 Menu 2 [BD x265 10bit AC3][9D6EAAFD].mkv',
                     '[RUBaDUB][1080p] Call of the Night - S01 Loss Time insert song [BD x265 10bit SUB AC3][B452145A].mkv',
                     'NC/ED.mkv', 'NC/OP.mkv', 'OP.mkv', 'S01OP-Daten [Creepy Nuts].mkv',
                     'S01ED-Yofukashi no Uta [Creepy Nuts].mkv'):
            with self.subTest(name=name):
                self.assertIsNone(self.episode(name, 1))
        self.assertEqual(self.episode('[RUBaDUB][1080p] Call of the Night - S01 E05 [BD x265 10bit Dual Audio AC3][79AD46BB].mkv', 1), 5)
        # A labeled extra that also declares a regular episode is still a conflict.
        with self.assertRaises(BatchPlanningError):
            self.episode('Show S01E05 Clean Opening.mkv', 1)

    def test_ordinal_episode_title_after_the_tag_is_not_a_range(self):   # EMBER, TTGA Call of the Night
        self.assertEqual(self.episode('S01E01-1st Night Night Flight [170B4E00].mkv', 1), 1)
        self.assertEqual(self.episode('S01E12-12th Night My Mom\'s Out Tonight [F06097E6].mkv', 1), 12)
        self.assertEqual(self.episode('S01E03-3rd Night A Lot Came Out.mkv', 1), 3)
        with self.assertRaises(BatchPlanningError):
            self.episode('Show S01E01-02.mkv', 1)

    def test_sidecar_audio_and_subtitle_tracks_are_allowed(self):   # 782, 844
        from src.anime.acquisition.metadata import MetadataInvalid
        files = inventory('Pack/Show - S01E01.mkv', 'Pack/Show - S01E01.mka', 'Pack/Show - S01E01.mks')
        self.assertEqual(plan_verified_batch(files, season=1, episode_map={1: 1}).probe_index, 0)
        with self.assertRaises(MetadataInvalid):
            inventory('Pack/Show - S01E01.mkv', 'Pack/setup.exe')


if __name__ == '__main__':
    unittest.main()
