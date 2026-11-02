"""Offline Prowlarr backfill search and episode catalog tests. No service or CI required."""
import datetime
import io
import json
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit

from src.nextgen.acquisition.prowlarr_search import (
    Client, DiscoveryPolicy, EpisodeCatalog, EpisodeTarget, Options, SearchConfiguration,
    SearchReview, SearchTimeout, SearchUnauthorized, SearchUnavailable, matches_episode,
    resolution, search_aliases,
)
from src.nextgen.providers.backfill_catalog import build_catalog
from src.nextgen.providers.parsing import episode_airdates
from src.nextgen.sonarr_episode import VerifiedEpisode

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
TITLES = ('Kusuriya no Hitorigoto', 'The Apothecary Diaries', '薬屋のひとりごと')


class Response(io.BytesIO):
    def __init__(self, data, code=200):
        super().__init__(json.dumps(data).encode())
        self.code = code

    def getcode(self):
        return self.code


def row(title, digest=A, **changes):
    base = {'title': title, 'indexer': 'Nyaa', 'protocol': 'torrent', 'seeders': 40,
            'size': 300_000_000, 'infoHash': digest.upper(),
            'magnetUrl': 'magnet:?xt=urn:btih:' + digest}
    base.update(changes)
    return base


class FakeProwlarr:
    def __init__(self, results=None):
        self.results = results if results is not None else {}
        self.queries = []
        self.error = None

    def __call__(self, request, timeout):
        assert request.get_method() == 'GET' and request.data is None
        assert request.get_header('X-api-key') == 'private-prowlarr-key'
        assert timeout == 45
        url = urlsplit(request.full_url)
        assert url.path == '/api/v1/search'
        query = parse_qs(url.query)
        assert query['type'] == ['search']
        self.queries.append(query['query'][0])
        if self.error: raise self.error
        return Response(self.results.get(query['query'][0], []))


def catalog(*targets):
    return EpisodeCatalog(TITLES, {t.tvdb_episode: t for t in targets})


SEARCHABLE = (EpisodeTarget(1, 'SEARCHABLE', 1, 9001), EpisodeTarget(2, 'SEARCHABLE', 2, 9002))


def client(fake, *targets, policy=None, sleeps=None):
    found = catalog(*(targets or SEARCHABLE))
    return Client(Options('http://prowlarr.local:9696', 45), 'private-prowlarr-key',
                  catalog=lambda aid, tid, season: found, transport=fake,
                  policy=policy or DiscoveryPolicy(),
                  sleep=(sleeps.append if sleeps is not None else lambda seconds: None))


class ReleaseMatchingTests(unittest.TestCase):
    def test_resolution_comes_from_the_title(self):
        self.assertEqual(resolution('[SubsPlease] Show - 01 (1080p) [ABCD].mkv'), 1080)
        self.assertEqual(resolution('Show S01E01 720p WEB-DL'), 720)
        self.assertEqual(resolution('Show - 01 [4K HEVC]'), 2160)
        self.assertEqual(resolution('Show - 01 [WEB]'), 0)
        self.assertEqual(resolution('Show - 01 [720p][1080p]'), 0)

    def test_title_alias_and_episode_must_both_match(self):
        target = EpisodeTarget(13, 'SEARCHABLE', 1, 9013)  # TVDB S02E13 is AniDB episode 1.
        self.assertTrue(matches_episode('[Group] Kusuriya no Hitorigoto - 01 (1080p)', TITLES, target, 2))
        self.assertTrue(matches_episode('The Apothecary Diaries S02E13 1080p WEB', TITLES, target, 2))
        self.assertFalse(matches_episode('[Group] Kusuriya no Hitorigoto - 02 (1080p)', TITLES, target, 2))
        self.assertFalse(matches_episode('The Apothecary Diaries S01E13 1080p', TITLES, target, 2))
        self.assertFalse(matches_episode('[Group] Other Show - 01 (1080p)', TITLES, target, 2))
        self.assertFalse(matches_episode('[Group] Kusuriya no Hitorigoto (Batch) (1080p)', TITLES, target, 2))
        self.assertFalse(matches_episode('[Group] Kusuriya no Hitorigoto - 01-12 (1080p)', TITLES, target, 2))

    def test_search_aliases_are_latin_distinct_and_bounded(self):
        self.assertEqual(search_aliases(TITLES + ('kusuriya no hitorigoto', 'Extra', 'More')),
                         ('Kusuriya no Hitorigoto', 'The Apothecary Diaries', 'Extra'))


class ProwlarrClientTests(unittest.TestCase):
    def test_each_episode_reports_its_own_result_and_ranks_locally(self):
        fake = FakeProwlarr({
            'Kusuriya no Hitorigoto 01': [
                row('[A] Kusuriya no Hitorigoto - 01 (720p)', A, seeders=500),
                row('[B] Kusuriya no Hitorigoto - 01 (1080p)', B, seeders=20),
                row('[C] Kusuriya no Hitorigoto - 01 (1080p)', C, seeders=2),
                row('[D] Kusuriya no Hitorigoto - 02 (1080p)', 'd' * 40)],
            'The Apothecary Diaries 01': [row('[B] Kusuriya no Hitorigoto - 01 (1080p)', B, seeders=20)],
        })
        targets = SEARCHABLE + (EpisodeTarget(3, 'NOT_AIRED'), EpisodeTarget(4, 'MAPPING_MISSING'))
        observed = client(fake, *targets).backfill(
            50, 100, 1, (1, 2, 3, 4), select_candidates=True)
        self.assertEqual([(o.number, o.status, o.candidates, o.accepted) for o in observed],
                         [(1, 'CANDIDATES', 3, 2), (2, 'NO_RESULTS', 0, 0),
                          (3, 'NOT_AIRED', 0, 0), (4, 'MAPPING_MISSING', 0, 0)])
        choice = observed[0].selection
        self.assertEqual((choice.release.info_hash, choice.release.resolution), (B, 1080))
        self.assertEqual(choice.identity, VerifiedEpisode(100, 9001, 1, 1))
        self.assertEqual([r.info_hash for r in choice.result.candidates], [B, A, C])
        # Episodes that cannot be searched never contact Prowlarr.
        self.assertEqual(fake.queries, ['Kusuriya no Hitorigoto 01', 'The Apothecary Diaries 01',
                                        'Kusuriya no Hitorigoto 02', 'The Apothecary Diaries 02'])

    def test_yearless_s02e02_indexer_release_is_returned_by_prowlarr(self):
        titles = ('Koori no Jouheki (2026)', 'The Ramparts of Ice (2026)')
        target = EpisodeTarget(2, 'SEARCHABLE', 2, 9002, 14)
        fake = FakeProwlarr({
            'The Ramparts of Ice S02E02': [
                row('[ToonsHub] The Ramparts of Ice S02E02 1080p WEB', A)
            ],
        })
        test_client = Client(Options('http://prowlarr.local:9696', 45),
                             'private-prowlarr-key',
                             catalog=lambda aid, tid, season: EpisodeCatalog(titles, {2: target}),
                             transport=fake)
        observed = test_client.backfill(20168, 459627, 2, (2,), select_candidates=True)
        self.assertEqual([(v.status, v.candidates, v.accepted) for v in observed],
                         [('CANDIDATES', 1, 1)])
        self.assertEqual(observed[0].selection.release.info_hash, A)
        self.assertIn('The Ramparts of Ice S02E02', fake.queries)

    def test_reserved_episode_is_not_searched(self):
        fake = FakeProwlarr()
        observed = client(fake).backfill(50, 100, 1, (1, 2), reserved=(1, 2))
        self.assertEqual([o.status for o in observed], ['IN_PROGRESS', 'IN_PROGRESS'])
        self.assertEqual(fake.queries, [])

    def test_conflicting_known_episode_id_is_not_searched(self):
        fake = FakeProwlarr()
        observed = client(fake).backfill(50, 100, 1, (1,), verified_ids={1: 777})
        self.assertEqual(observed[0].status, 'MAPPING_MISSING')
        self.assertEqual(fake.queries, [])

    def test_releases_over_the_size_limit_are_never_sent(self):
        title = '[A] Kusuriya no Hitorigoto - 01 '
        fake = FakeProwlarr({'Kusuriya no Hitorigoto 01': [
            row(title + '(1080p)', A, size=1_420_000_000, seeders=900),
            row(title + '[1080p HEVC]', B, size=380_000_000, seeders=40),
            row(title + '(720p)', C, size=0, seeders=40)]})   # Unknown size is not skipped.
        observed = client(fake, policy=DiscoveryPolicy(1080, 5, 'resolution', 600)).backfill(
            50, 100, 1, (1,), select_candidates=True)
        self.assertEqual((observed[0].candidates, observed[0].accepted), (3, 2))
        self.assertEqual([r.info_hash for r in observed[0].selection.result.candidates], [B, C, A])
        self.assertEqual(observed[0].selection.release.info_hash, B)

    def test_magnet_sources_and_conflicts(self):
        title = '[A] Kusuriya no Hitorigoto - 01 (1080p)'
        fake = FakeProwlarr({'Kusuriya no Hitorigoto 01': [
            row(title, A, magnetUrl=None, guid='magnet:?xt=urn:btih:' + A),
            row(title, B, magnetUrl=None),
            row(title, C, infoHash=A),
            row(title, 'e' * 40, protocol='usenet'),
        ]})
        ranked = client(fake).search(50, VerifiedEpisode(100, 9001, 1, 1)).candidates
        self.assertEqual([(r.info_hash, r.magnet_url) for r in ranked],
                         [(A, 'magnet:?xt=urn:btih:' + A), (B, 'magnet:?xt=urn:btih:' + B)])

    def test_search_rejects_a_changed_episode_identity(self):
        with self.assertRaises(SearchReview):
            client(FakeProwlarr()).search(50, VerifiedEpisode(100, 1234, 1, 1))

    def test_policy_limits(self):
        title = '[A] Kusuriya no Hitorigoto - 01 '
        fake = FakeProwlarr({'Kusuriya no Hitorigoto 01': [
            row(title + '(2160p)', A), row(title + '(1080p)', B, seeders=None),
            row(title + '[WEB]', C)]})
        observed = client(fake, policy=DiscoveryPolicy(1080, 5, 'resolution')).backfill(50, 100, 1, (1,))
        self.assertEqual((observed[0].status, observed[0].candidates, observed[0].accepted),
                         ('CANDIDATES', 3, 0))

    def test_slow_or_busy_prowlarr_gets_two_more_tries(self):
        for error, expected in ((TimeoutError(), SearchTimeout),
                                (urllib.error.URLError(TimeoutError()), SearchTimeout),
                                (urllib.error.URLError('refused'), SearchUnavailable),
                                (urllib.error.HTTPError('u', 503, 'x', {}, io.BytesIO()), SearchUnavailable)):
            fake, sleeps = FakeProwlarr(), []
            fake.error = error
            with self.subTest(error=error), self.assertRaises(expected):
                client(fake, sleeps=sleeps).backfill(50, 100, 1, (1, 2))
            self.assertEqual(len(fake.queries), 3)
            self.assertEqual(sleeps, [15, 60])

    def test_authentication_failure_is_not_retried(self):
        fake, sleeps = FakeProwlarr(), []
        fake.error = urllib.error.HTTPError('u', 401, 'x', {}, io.BytesIO())
        with self.assertRaises(SearchUnauthorized):
            client(fake, sleeps=sleeps).backfill(50, 100, 1, (1,))
        self.assertEqual((len(fake.queries), sleeps), (1, []))

    def test_rate_limit_honors_retry_after_and_recovers(self):
        class Busy(FakeProwlarr):
            def __call__(self, request, timeout):
                if len(self.queries) < 2:
                    self.queries.append('busy')
                    raise urllib.error.HTTPError('u', 429, 'busy', {'Retry-After': '500' if self.queries == ['busy'] else '7'},
                                                 io.BytesIO())
                return super().__call__(request, timeout)
        fake, sleeps = Busy({'Kusuriya no Hitorigoto 01': [row('[A] Kusuriya no Hitorigoto - 01 (1080p)', A)]}), []
        observed = client(fake, sleeps=sleeps).backfill(50, 100, 1, (1,))
        self.assertEqual(sleeps, [120, 7])       # Retry-After, capped at two minutes.
        self.assertEqual(observed[0].status, 'CANDIDATES')

    def test_invalid_response_needs_review(self):
        class Bad(FakeProwlarr):
            def __call__(self, request, timeout):
                return Response({'not': 'a list'})
        with self.assertRaises(SearchReview):
            client(Bad()).backfill(50, 100, 1, (1,))

    def test_configuration_and_secret_hygiene(self):
        for options in (('', 30), ('ftp://host', 30), ('http://user:pw@host', 30),
                        ('http://host', 0), ('http://host', 301)):
            with self.subTest(options=options), self.assertRaises(SearchConfiguration):
                Options(*options)
        with self.assertRaises(SearchConfiguration):
            Client(Options('http://host'), '', catalog=lambda *a: None)
        secret = Client(Options('http://host'), 'private-prowlarr-key', catalog=lambda *a: None)
        self.assertNotIn('private-prowlarr-key', repr(secret.__dict__.get('options')))
        self.assertNotIn('http://host', repr(Options('http://host')))


class FakeProviders:
    """Cached AniDB/Anime-Lists/TVDB answers in the providers.http client shape."""
    def __init__(self, *, mapping, tvdb, airdates):
        self._mapping, self._tvdb, self._airdates = mapping, tvdb, airdates

    def anime(self, aid):
        return {'anidb_id': aid, 'title': 'Kusuriya no Hitorigoto', 'titles': TITLES}

    def episodes(self, aid):
        return {n: 5000 + n for n in self._airdates}

    def airdates(self, aid):
        return self._airdates

    def mapping_xml(self):
        return self._mapping

    def episode_page(self, tid, page, secret):
        return self._tvdb, False


class BackfillCatalogTests(unittest.TestCase):
    def test_offsets_air_dates_and_missing_mappings(self):
        # AniDB sequel episodes 1-3 are TVDB S01E13-S01E15 (continuous TVDB season).
        mapping = (b'<anime-list><anime anidbid="50" tvdbid="100" defaulttvdbseason="1" '
                   b'episodeoffset="12"/></anime-list>')
        providers = FakeProviders(
            mapping=mapping,
            tvdb=((9013, 1, 13), (9014, 1, 14), (9015, 1, 15), (9016, 1, 16), (8001, 2, 1)),
            airdates={1: '2026-10-01', 2: '2026-10-08', 3: '2026-11-05'})
        found = build_catalog(providers, 50, 100, 1, None, today=datetime.date(2026, 10, 29))
        self.assertEqual(found.titles, TITLES)
        self.assertEqual(found.target(13), EpisodeTarget(13, 'SEARCHABLE', 1, 9013, 13))
        self.assertEqual(found.target(14), EpisodeTarget(14, 'SEARCHABLE', 2, 9014, 14))
        self.assertEqual(found.target(15), EpisodeTarget(15, 'NOT_AIRED', 3, 9015, 15))
        self.assertEqual(found.target(16).status, 'MAPPING_MISSING')  # TVDB lists it; AniDB does not.
        self.assertEqual(found.target(20).status, 'NOT_AIRED')        # Not listed anywhere yet.

    def test_anidb_air_dates_are_parsed_for_regular_episodes_only(self):
        payload = ('<anime id="50"><episodes>'
                   '<episode id="1"><epno type="1">1</epno><airdate>2026-10-01</airdate></episode>'
                   '<episode id="2"><epno type="1">2</epno></episode>'
                   '<episode id="3"><epno type="2">1</epno><airdate>2026-10-01</airdate></episode>'
                   '</episodes></anime>').encode()
        self.assertEqual(episode_airdates(payload, 50), {1: '2026-10-01', 2: None})


class ProwlarrSettingsTests(unittest.TestCase):
    def test_v10_discovery_preferences_move_to_prowlarr(self):
        from src.nextgen.configuration_schema import ConfigurationError, convert_document
        doc = {'schema_version': 10, 'revision': 4, 'settings': {'sonarr': {
            'base_url': 'http://sonarr.local:8989', 'discovery_max_resolution': 720,
            'discovery_min_seeders': 12, 'discovery_rank_by': 'seeders'}}}
        upgraded = convert_document(doc)
        self.assertEqual((upgraded.schema_version, upgraded.revision), (13, 7))
        self.assertEqual((upgraded.value('prowlarr.max_resolution'), upgraded.value('prowlarr.min_seeders'),
                          upgraded.value('prowlarr.rank_by')), (720, 12, 'seeders'))
        self.assertEqual(upgraded.value('prowlarr.base_url'), '')
        self.assertEqual(upgraded.value('sonarr.base_url'), 'http://sonarr.local:8989')
        self.assertNotIn('discovery_min_seeders', upgraded.to_document()['settings']['sonarr'])
        with self.assertRaises(ConfigurationError):
            convert_document({**doc, 'settings': {'prowlarr': {'base_url': 'http://p'}}})
        with self.assertRaises(ConfigurationError):
            convert_document({'schema_version': 11, 'revision': 1,
                              'settings': {'sonarr': {'discovery_min_seeders': 1}}})

    def test_policy_and_options_read_the_prowlarr_settings(self):
        from src.nextgen.configuration_schema import patch_snapshot, ConfigurationSnapshot
        current, _ = patch_snapshot(ConfigurationSnapshot(1, {}), {'prowlarr': {
            'base_url': 'http://prowlarr.local:9696', 'timeout_seconds': 90,
            'max_resolution': 2160, 'min_seeders': 1, 'rank_by': 'seeders'}})
        self.assertEqual(Options.capture(current), Options('http://prowlarr.local:9696', 90))
        self.assertEqual(DiscoveryPolicy.capture(current), DiscoveryPolicy(2160, 1, 'seeders', 600, True))

    def test_secret_store_keeps_the_prowlarr_key_private(self):
        import tempfile, os
        from src.nextgen.secrets import SecretStore
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            store = SecretStore(directory)
            store.initialize()
            with self.assertRaises(SearchConfiguration):
                store.prowlarr_api_key()
            result = store.update_prowlarr_api_key('private-prowlarr-key', expected_revision=1)
            self.assertTrue(result.status.prowlarr_configured)
            self.assertEqual(store.prowlarr_api_key(), 'private-prowlarr-key')
            self.assertNotIn('private-prowlarr-key', repr(store.status()))


if __name__ == '__main__':
    unittest.main()
