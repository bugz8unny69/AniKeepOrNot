"""Season-aware NextGen title discovery remains strict about provider identity."""
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from src.anime.matching import MatchingTrace
from src.anime.providers.http import Credentials, Options
from src.anime.providers.identity import explicit_sequel_alias, make_resolver
from src.anime.storage.sqlite import IdentityConflict, IdentityNotReady
from src.anime.storage.migrations import LATEST_VERSION


RELEASE = "[Dyna] Higurashi When They Cry S02 (BD 1080p AVC Opus 2.0) [Dual Audio] | Higurashi Sotsu"


_DATABASE = None


def _database():
    """One empty, current database for the module: identification reads hand matches and
    recognized seasons from it (there are none here), so it must exist."""
    global _DATABASE
    if _DATABASE is None:
        import atexit, shutil, sqlite3, tempfile
        from src.anime.storage.sqlite import initialize
        folder = tempfile.mkdtemp(prefix="sequel-discovery-")
        atexit.register(shutil.rmtree, folder, True)
        _DATABASE = folder + "/akon.sqlite"
        db = sqlite3.connect(_DATABASE)
        initialize(db)
        db.close()
    return _DATABASE


class Configuration:
    values = {"identity.match_threshold_pct": 90}

    def value(self, name):
        if name == "storage.database_path":
            return _database()
        if name in self.values:
            return self.values[name]
        raise AssertionError(name)


class Delivery:
    def __init__(self, name):
        self.raw_body = json.dumps({"release_name": name}).encode()


class Provider:
    def __init__(self):
        self.queries = []
        self.season_lookups = []
        self.candidates = {15105: ("Higurashi Sotsu",)}
        self.tvdb_titles = ("Higurashi Sotsu",)
        self.tvdb_anidb_ids = frozenset({15105})
        self.season_entry_id = 17000

    def title_candidates(self, aliases):
        self.queries.append(tuple(aliases))
        return self.candidates

    def anime(self, aid):
        if aid == 17000:
            return {"title": "Youjo Senki II", "titles": ("Youjo Senki II",)}
        return {"title": "Higurashi Sotsu", "titles": ("Higurashi Sotsu",)}

    def mapping(self, aid):
        return 12345

    def season_entry(self, tid, season, episode=None):
        self.season_lookups.append((tid, season, episode))
        return self.season_entry_id


    def series(self, tid, credentials):
        return {"titles": self.tvdb_titles, "animation": True, "live": False,
                "anidb_ids": self.tvdb_anidb_ids}


class SequelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_resolver(self, provider):
        return make_resolver(
            Options("nextgen_tests", 1), lambda: Credentials("dummy-test-key"),
            client_factory=lambda db, opts: provider,
        )

    async def identify(self, title, provider):
        trace = MatchingTrace()
        db_patch = patch("src.anime.providers.identity._open")
        recognized_patch = patch(
            "src.anime.providers.identity.recognized_for_release",
            return_value=None,
        )
        with db_patch as opener, recognized_patch:
            opener.return_value.execute.return_value.fetchone.return_value = (LATEST_VERSION,)
            result = await self.make_resolver(provider)(
                Delivery(title), Configuration(), threading.Event(), matching=trace
            )
        return result, trace.data

    async def test_explicit_s02_title_uses_distinct_sequel_lookup(self):
        provider = Provider()
        provider.season_entry_id = 15105   # The sequel work is TVDB season 2's entry.
        result, trace = await self.identify(RELEASE, provider)
        self.assertEqual(provider.queries, [("Higurashi Sotsu",)])
        self.assertEqual(result["anime"]["anidb_id"], 15105)
        self.assertEqual(trace["season"], 2)
        self.assertEqual(trace["queries"], ["Higurashi Sotsu"])
        self.assertIn(
            {"step": "season_scope", "status": "passed", "note": "sequel_alias_selected"},
            trace["checks"],
        )
        self.assertEqual(trace["selected_tvdb_id"], 12345)

    async def test_s01_keeps_original_lookup(self):
        provider = Provider()
        await self.identify("[Dyna] Higurashi When They Cry S01 E01 | Higurashi Sotsu", provider)
        self.assertEqual(provider.queries, [("Higurashi When They Cry",)])

    async def test_ambiguous_candidates_cannot_be_approved(self):
        provider = Provider()
        provider.candidates = {
            15105: ("Higurashi Sotsu",),
            15106: ("Higurashi Sotsu",),
        }
        with self.assertRaises(IdentityNotReady):
            await self.identify(RELEASE, provider)

    async def test_provider_disagreement_is_still_rejected(self):
        provider = Provider()
        provider.tvdb_titles = ("A Different Show",)
        provider.tvdb_anidb_ids = frozenset()
        with self.assertRaises(IdentityConflict):
            await self.identify(RELEASE, provider)

    async def test_sequel_entry_agrees_through_its_anime_lists_series_group(self):   # jobs 9194, 447
        provider = Provider()
        provider.season_entry_id = 15105
        provider.tvdb_titles = ("Higurashi When They Cry",)       # TVDB names the series, not the sequel
        provider.tvdb_anidb_ids = frozenset()
        provider.series_group_titles = lambda tid: ("Higurashi no Naku Koro ni", "Higurashi When They Cry")
        result, trace = await self.identify(RELEASE, provider)
        self.assertEqual(result["anime"]["anidb_id"], 15105)
        provider.series_group_titles = lambda tid: ("Some Other Series",)
        with self.assertRaises(IdentityConflict):
            await self.identify(RELEASE, provider)

    async def test_verified_provider_link_allows_different_title_language(self):
        # W-13: JoJo / Black Clover scenario: TVDB lists explicit AniDB remoteId 15105
        # but title spelling differs ("Higurashi: When They Cry - SOTSU").
        provider = Provider()
        provider.tvdb_titles = ("Higurashi: When They Cry - SOTSU",)
        provider.season_entry_id = 15105
        result, trace = await self.identify(RELEASE, provider)
        self.assertEqual(result["anime"]["anidb_id"], 15105)
        self.assertEqual(result["anime"]["tvdb_series_id"], 12345)

    async def test_plain_s02_title_uses_anime_lists_season_entry(self):
        # No "| Sequel" suffix: identify the base series, then take its season 2 entry.
        provider = Provider()
        provider.candidates = {15105: ("Saga of Tanya the Evil",)}
        provider.anime = lambda aid: ({"title": "Youjo Senki II", "titles": ("Youjo Senki II",)}
            if aid == 17000 else {"title": "Youjo Senki", "titles": ("Youjo Senki", "Saga of Tanya the Evil")})
        provider.tvdb_titles = ("Saga of Tanya the Evil",)
        result, trace = await self.identify(
            "Saga of Tanya the Evil S02E08 Torpedo Strike 1080p CR WEB-DL DUAL AAC2.0 H.264-VARYG "
            "(Youjo Senki II, Dual-Audio, Multi-Subs)", provider)
        # The bracketed alternate title is searched too; the metadata parts are not.
        self.assertEqual(provider.queries, [("Saga of Tanya the Evil", "Youjo Senki II")])
        self.assertEqual(provider.season_lookups, [(12345, 2, 8)])
        self.assertEqual(result["anime"], {"anidb_id": 17000, "tvdb_series_id": 12345,
                                           "title": "Youjo Senki II"})
        self.assertIn({"step": "season_scope", "status": "passed",
                       "note": "base_series_season_lookup"}, trace["checks"])

    async def test_unknown_sequel_title_falls_back_to_base_series_season(self):
        # AniDB has no exact entry for the release's "| Sequel II" title.
        provider = Provider()
        provider.title_candidates = lambda aliases: (provider.queries.append(tuple(aliases)) or
            ({15105: ("The Iceblade Sorcerer Shall Rule the World",)}
             if aliases == ["The Iceblade Sorcerer Shall Rule the World"] else {}))
        provider.anime = lambda aid: {"title": "Hyouken II" if aid == 17000 else "Hyouken",
            "titles": ("The Iceblade Sorcerer Shall Rule the World",)}
        provider.tvdb_titles = ("The Iceblade Sorcerer Shall Rule the World",)
        result, trace = await self.identify(
            "[AnoZu] The Iceblade Sorcerer Shall Rule the World S02E02 1080p CR WEB-DL Dual-Audio "
            "DDP 2.0 H.264 | Hyouken no Majutsushi ga Sekai wo Suberu II", provider)
        self.assertEqual(provider.queries, [("Hyouken no Majutsushi ga Sekai wo Suberu II",),
                                            ("The Iceblade Sorcerer Shall Rule the World",)])
        self.assertEqual(provider.season_lookups, [(12345, 2, 2)])
        self.assertEqual(result["anime"]["anidb_id"], 17000)
        self.assertIn({"step": "season_scope", "status": "passed",
                       "note": "base_series_season_lookup"}, trace["checks"])

    async def test_season_lookup_still_requires_base_title_agreement(self):
        provider = Provider()
        provider.candidates = {15105: ("Saga of Tanya the Evil",)}
        provider.anime = lambda aid: {"title": "Youjo Senki", "titles": ("Saga of Tanya the Evil",)}
        provider.tvdb_titles = ("A Different Show",)
        provider.tvdb_anidb_ids = frozenset()
        with self.assertRaises(IdentityConflict):
            await self.identify("Saga of Tanya the Evil S02E08 1080p", provider)

    async def test_s01_pipe_alternative_after_primary_miss(self):
        provider = Provider()
        def lookup(aliases):
            provider.queries.append(tuple(aliases))
            return ({15105: ("Higurashi Gou",)}
                    if aliases == ["Higurashi Gou"] else {})
        provider.title_candidates = lookup
        provider.anime = lambda aid: {"title": "Higurashi Gou",
                                      "titles": ("Higurashi Gou",)}
        provider.tvdb_titles = ("Higurashi Gou",)
        result, trace = await self.identify(
            "[Dyna] Higurashi When They Cry S01 (BD 1080p AVC Opus 2.0) "
            "[Dual Audio] | Higurashi Gou", provider)
        self.assertEqual(provider.queries,
                         [("Higurashi When They Cry",), ("Higurashi Gou",)])
        self.assertEqual(trace["queries"],
                         ["Higurashi When They Cry", "Higurashi Gou"])
        self.assertEqual(result["anime"]["anidb_id"], 15105)

    async def test_s03_noisy_sequel_falls_back_to_base(self):
        provider = Provider()
        def lookup(aliases):
            provider.queries.append(tuple(aliases))
            return ({15105: ("Mushoku Tensei: Jobless Reincarnation",)}
                    if aliases == ["Mushoku Tensei Jobless Reincarnation"] else {})
        provider.title_candidates = lookup
        provider.anime = lambda aid: (
            {"title": "Mushoku Tensei III", "titles": ("Mushoku Tensei III",)}
            if aid == 17000 else
            {"title": "Mushoku Tensei: Jobless Reincarnation",
             "titles": ("Mushoku Tensei: Jobless Reincarnation",)})
        provider.tvdb_titles = ("Mushoku Tensei: Jobless Reincarnation",)
        result, trace = await self.identify(
            "[Sokudo] Mushoku Tensei Jobless Reincarnation S03 P1 v2 "
            "[1080p Dual Audio AV1] | Mushoku Tensei III: Isekai Ittara "
            "Honki Dasu, Dual-Audio, Multi-Subs, Batch)", provider)
        self.assertEqual(provider.queries, [
            ("Mushoku Tensei III: Isekai Ittara Honki Dasu",),
            ("Mushoku Tensei Jobless Reincarnation",),
        ])
        self.assertEqual(provider.season_lookups, [(12345, 3, None)])
        self.assertEqual(result["anime"]["anidb_id"], 17000)
        self.assertEqual(trace["queries"], [
            "Mushoku Tensei III: Isekai Ittara Honki Dasu",
            "Mushoku Tensei Jobless Reincarnation",
        ])

    def test_anime_lists_season_entry_selection(self):
        from src.anime.providers.parsing import season_entry
        payload = (b'<anime-list>'
            b'<anime anidbid="100" tvdbid="5" defaulttvdbseason="1"/>'
            b'<anime anidbid="200" tvdbid="5" defaulttvdbseason="2"/>'
            b'<anime anidbid="201" tvdbid="5" defaulttvdbseason="2" episodeoffset="12"/>'
            b'<anime anidbid="300" tvdbid="5" defaulttvdbseason="a"/>'
            b'<anime anidbid="400" tvdbid="6" defaulttvdbseason="2"/>'
            b'</anime-list>')
        self.assertEqual(season_entry(payload, 5, 2, 8), 200)    # first cour
        self.assertEqual(season_entry(payload, 5, 2, 15), 201)   # split second cour
        self.assertEqual(season_entry(payload, 6, 2), 400)
        with self.assertRaises(IdentityNotReady):
            season_entry(payload, 5, 3, 1)
        with self.assertRaises(IdentityConflict):
            season_entry(payload.replace(b'episodeoffset="12"', b''), 5, 2, 8)

    def test_no_distinct_sequel_title_is_not_first_season_proof(self):
        self.assertIsNone(explicit_sequel_alias(
            "[Dyna] Higurashi When They Cry S02 (BD 1080p)",
            ["Higurashi When They Cry"],
        ))
        self.assertIsNone(explicit_sequel_alias(
            "Higurashi When They Cry S02 | BD 1080p",
            ["Higurashi When They Cry"],
        ))



class SeasonEntryCorrectionTests(unittest.IsolatedAsyncioTestCase):
    """The title finds the right series but not the season's entry (prod jobs 751, 1181)."""
    make_resolver = SequelDiscoveryTests.make_resolver
    identify = SequelDiscoveryTests.identify

    def provider(self, candidates, titles, entries):
        provider = Provider()
        provider.candidates = candidates
        provider.anime = lambda aid: {"title": titles[aid], "titles": (titles[aid],)}
        provider.tvdb_titles = tuple(titles.values())
        provider.tvdb_anidb_ids = frozenset(titles)
        def season_entry(tid, season, episode=None):
            provider.season_lookups.append((tid, season, episode))
            if (season, episode) not in entries:
                raise IdentityNotReady()
            return entries[season, episode]
        provider.season_entry = season_entry
        return provider

    async def test_alternate_title_of_the_first_season_moves_to_season_four(self):
        provider = self.provider({13871: ("Tensei shitara Slime Datta Ken",)},
            {13871: "Tensei shitara Slime Datta Ken", 18884: "Tensei Shitara Slime Datta Ken Dai 4 Ki"},
            {(4, None): 18884})
        result, _ = await self.identify("[FoundYears] That Time I Got Reincarnated as a Slime Season 4 "
            "(CR WEB-DL 1080p AVC E-AC3) [Dual-Audio] | Tensei shitara Slime Datta Ken", provider)
        self.assertEqual(result["anime"]["anidb_id"], 18884)
        self.assertEqual(provider.season_lookups, [(12345, 4, None)])

    async def test_second_cour_pack_moves_to_the_cour_two_entry(self):
        provider = self.provider({16128: ("AMAIM Warrior at the Borderline",)},
            {16128: "AMAIM Warrior at the Borderline", 17091: "Kyoukai Senki (2022)"},
            {(1, 14): 17091})
        result, _ = await self.identify("AMAIM Warrior at the Borderline S01E14-E25 (Cour 2) "
            "[CR Web-DL 1080p AVC Opus] [Dual Audio] [hchcsen]", provider)
        self.assertEqual(result["anime"]["anidb_id"], 17091)

    def jojo(self, extra_entries=()):
        """Anime-Lists as on production: 788/789/4918 are TVDB 83950 (OVAs),
        9304 is TVDB 262954 (2012 TV) whose season 6 is 19287 Steel Ball Run."""
        titles = {788: "JoJo no Kimyou na Bouken", 789: "JoJo no Kimyou na Bouken (2000)",
                  4918: "JoJo no Kimyou na Bouken: Phantom Blood", 9304: "JoJo no Kimyou na Bouken (2012)",
                  19287: "Steel Ball Run: JoJo no Kimyou na Bouken"}
        series = {788: 83950, 789: 83950, 4918: 83950, 9304: 262954, 19287: 262954}
        entries = {(83950, 1): 788, (83950, 2): 789, (83950, 0): 4918, (262954, 1): 9304,
                   (262954, 6): 19287, **dict(extra_entries)}
        provider = Provider()
        provider.candidates = {aid: ("JoJo's Bizarre Adventure", title) for aid, title in titles.items()
                               if aid != 19287}
        provider.anime = lambda aid: {"title": titles[aid], "titles": (titles[aid], "JoJo's Bizarre Adventure")}
        provider.mapping = lambda aid: series[aid]
        provider.tvdb_titles = ("JoJo's Bizarre Adventure",)
        provider.tvdb_anidb_ids = frozenset({9304})
        def season_entry(tid, season, episode=None):
            provider.season_lookups.append((tid, season, episode))
            if (tid, season) not in entries:
                raise IdentityNotReady()
            return entries[tid, season]
        provider.season_entry = season_entry
        return provider

    async def test_tied_title_takes_the_season_from_the_candidate_whose_series_has_it(self):
        # Prod jobs 542, 548, 549, 8758, 8764, 9988, 10004: 788 ranks first, but
        # only 9304's Anime-Lists series (262954) has a season 6.
        for name in ("[ToonsHub] JoJos Bizarre Adventure S06E06 1080p NF WEB-DL DUAL AAC2.0 H.264 "
                     "(JoJo no Kimyou na Bouken: Steel Ball Run, Dual-Audio, Multi-Subs)",
                     "[Judas] JoJo's Bizarre Adventure (JoJo no Kimyou na Bouken) - S06E09 [1080p][HEVC x265 10bit]"
                     "[Dual-Audio][Multi-Subs] (Weekly)"):
            with self.subTest(name=name):
                provider = self.jojo()
                result, trace = await self.identify(name, provider)
                self.assertEqual((result["anime"]["anidb_id"], result["anime"]["tvdb_series_id"]), (19287, 262954))
                self.assertIn("season_entry_other_candidate", [c["note"] for c in trace["checks"]])

    async def test_two_candidate_series_with_the_season_stay_in_review(self):
        # 788's series has no season 6; two other candidates' series both have one.
        provider = self.jojo(extra_entries={(99999, 6): 20000})
        provider.candidates[20000] = ("JoJo's Bizarre Adventure", "JoJo Other Work")
        mapping = provider.mapping
        provider.mapping = lambda aid: 99999 if aid == 20000 else mapping(aid)
        with self.assertRaises(IdentityNotReady):
            await self.identify("JoJos Bizarre Adventure S06E06 1080p", provider)

    async def test_part_number_and_subtitle_find_the_series(self):
        # Prod jobs 385, 9066: the full title scored below the threshold.
        provider = self.jojo()
        result, _ = await self.identify("[Anime Chap] JoJo's Bizarre Adventure Part 7: Steel Ball Run S06E06 "
                                        "[WEB 1080p] {Corrected Names} Episode 06 [Dual Audio]", provider)
        self.assertEqual(result["anime"]["anidb_id"], 19287)

    async def test_unsupported_releases_raise_their_own_reason(self):
        from src.anime.storage.sqlite import AniDBEntryMissing, AnimeListsEntryMissing
        # Prod jobs 154-158: the name carries no title at all.
        with self.assertRaises(AniDBEntryMissing):
            await self.identify("T00393120 S01E02 1080p CR WEB-DL DUAL AAC2.0 H.264-VARYG (Dual-Audio, Multi-Subs)",
                                Provider())
        # Prod jobs 8384, 8548, 481, 9238: AniDB lists nothing close enough.
        provider = Provider()
        provider.candidates = {}
        with self.assertRaises(AniDBEntryMissing):
            await self.identify("[ToonsHub] Tom and Jerry Forbidden Compass (2025) 1080p AMZN WEB-DL DUAL", provider)
        provider.candidates = {15668: ("Tom Sawyer no Bouken",)}   # A fuzzy hit below the threshold.
        with self.assertRaises(AniDBEntryMissing):
            await self.identify("[ToonsHub] Tom and Jerry Forbidden Compass (2025) 1080p AMZN WEB-DL DUAL", provider)
        # Prod jobs 8668, 8950, 10067, 332: on AniDB, not in Anime-Lists.
        provider = Provider()
        provider.candidates = {20379: ("Aoki Denshou: Welsh & Shedar",)}
        provider.anime = lambda aid: {"title": "Aoki Denshou: Welsh & Shedar", "titles": ("Aoki Denshou: Welsh & Shedar",)}
        def mapping(aid): raise IdentityNotReady()
        provider.mapping = mapping
        with self.assertRaises(AnimeListsEntryMissing):
            await self.identify("[ToonsHub] Welsh and Shedar S01E03 1080p AMZN WEB-DL DUAL DDP2.0 H.264 "
                                "(Aoki Denshou: Welsh & Shedar, Dual-Audio, Multi-Subs)", provider)

    async def test_anime_lists_words_instead_of_a_tvdb_id_are_unsupported(self):
        # Prod 9164 Look Back (tvdbid="movie"), 8572 (tvdbid="web"): these used to
        # read as a broken provider response, retry until exhausted, then review.
        from src.anime.providers import parsing
        from src.anime.storage.sqlite import AnimeListsEntryMissing, AnimeListsMovie
        lists = (b'<anime-list><anime anidbid="18493" tvdbid="movie"/>'
                 b'<anime anidbid="9983" tvdbid="web"/><anime anidbid="13" tvdbid="hentai"/></anime-list>')
        for aid, title, error in ((18493, "Look Back", AnimeListsMovie),
                                  (9983, "Houseki no Kuni", AnimeListsEntryMissing)):
            provider = Provider()
            provider.candidates = {aid: (title,)}
            provider.anime = lambda _, title=title: {"title": title, "titles": (title,)}
            provider.mapping = lambda a: parsing.mapping_id(lists, a)
            with self.subTest(title), self.assertRaises(error):
                await self.identify(title + " (BD 1080p HEVC Dual-Audio)", provider)
        self.assertEqual(AnimeListsMovie.reason_code, "RELEASE_MOVIE_SKIPPED")
        with self.assertRaises(AnimeListsEntryMissing):
            parsing.mapping_id(lists, 13)

    async def test_tied_entries_still_review(self):
        from src.anime.storage.sqlite import AniDBEntryMissing, AnimeListsEntryMissing
        provider = Provider()
        provider.candidates = {1: ("Same Show",), 2: ("Same Show",)}
        with self.assertRaises(IdentityNotReady) as raised:
            await self.identify("Same Show S01E01 1080p", provider)
        self.assertNotIsInstance(raised.exception, (AniDBEntryMissing, AnimeListsEntryMissing))

    async def test_no_single_season_entry_keeps_the_title_match(self):
        provider = self.provider({16128: ("AMAIM Warrior at the Borderline",)},
            {16128: "AMAIM Warrior at the Borderline"}, {})
        result, _ = await self.identify("AMAIM Warrior at the Borderline S01E14 1080p", provider)
        self.assertEqual(result["anime"]["anidb_id"], 16128)



class AliasFormTests(unittest.TestCase):
    """Real release titles that found no AniDB entry (production IDENTIFY_ANIME jobs)."""
    def forms(self, title):
        from src.anime.providers.identity import alias_forms
        from src.anime.release_names import release_title
        return alias_forms(title, release_title(title)[0])

    def test_cleaned_forms(self):
        cases = {
            "Magic.Kaito.1412.S01.REPACK.1080p.BluRay.REMUX.AVC.Dual-Audio.FLAC.2.0-Fool": "Magic Kaito 1412",
            "[AnoZu] LIAR GAME 2026 S01 1080p CR WEB-DL Dual-Audio DDP 2.0 H.264": "LIAR GAME",
            "Look Back (2024) (BD 1080p HEVC h265 Dual-Audio EAC3/Opus)": "Look Back",
            "[Moxie] Ao no Exorcist - Season 01 (S01) (BD Remux 1080p FLAC 2.0) [Dual Audio]": "Ao no Exorcist",
            "[URD] Tensei Shitara Dragon no Tamago Datta (Reincarnated as a Dragon Hatchling) (Season 01) (BD 1080p)":
                "Reincarnated as a Dragon Hatchling",
            "[ToonsHub] Welsh and Shedar S01E04 1080p AMZN WEB-DL DUAL DDP2.0 H.264 "
            "(Aoki Denshou: Welsh & Shedar, Dual-Audio, Multi-Subs)": "Aoki Denshou: Welsh & Shedar",
        }
        for title, wanted in cases.items():
            with self.subTest(title=title):
                forms = self.forms(title)
                self.assertIn(wanted, forms)
                self.assertFalse(any(f in ("Dual-Audio", "Multi-Subs") for f in forms))

    def test_subtitle_order_and_part_number(self):   # jobs 385, 9066
        forms = self.forms("[Anime Chap] JoJo's Bizarre Adventure Part 7: Steel Ball Run S06E06 [WEB 1080p]")
        self.assertIn("Steel Ball Run: JoJo's Bizarre Adventure", forms)
        self.assertIn("JoJo's Bizarre Adventure", forms)
        forms = self.forms("[Group] JoJo no Kimyou na Bouken: Steel Ball Run S06E06 [WEB 1080p]")
        self.assertIn("Steel Ball Run: JoJo no Kimyou na Bouken", forms)
        self.assertNotIn("JoJo no Kimyou na Bouken", forms)   # Without "Part N" the series title is not split off.

    def test_fuzzy_match_uses_words(self):   # job 9521: "Alyu" for "Alya"
        from src.anime.providers import parsing
        payload = ('<animetitles><anime aid="17914"><title type="official" xml:lang="en">'
                   'Alya Sometimes Hides Her Feelings in Russian</title></anime>'
                   '<anime aid="1"><title type="main" xml:lang="x-jat">Hidden Russian</title></anime>'
                   '</animetitles>').encode()
        found = parsing.title_matches(payload, ["Alyu Sometimes Hides Her Feelings in Russian"])
        self.assertEqual(set(found), {17914})


if __name__ == "__main__":
    unittest.main()
