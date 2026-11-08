"""Offline provider HTTP/cache/parsing cases using fake responses, unexecuted."""
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
import zlib

from src.anime.providers.errors import (ProviderAuthenticationFailed, ProviderDeferred,
    ProviderInvalidResponse, ProviderUnavailable)
from src.anime.providers.http import Clients, Credentials, MemoryCache, Options, decode, retry_after
from src.anime.providers.parsing import anime_record, episode_catalog, title_matches, mapping_id, tvdb_record
from src.anime.storage.provider_cache import ProviderStore
from src.anime.storage.sqlite import IdentityConflict, IdentityNotReady, initialize

ANIME = b'<anime id="123"><titles><title type="main">Test Anime</title></titles><episodes><episode id="987"><epno type="1">1</epno></episode></episodes></anime>'
TITLES = b'<animetitles><anime aid="123"><title>Test Anime</title></anime></animetitles>'
MAPPING = b'<anime-list><anime anidbid="123" tvdbid="456"/></anime-list>'
TVDB = {"data":{"id":456,"name":"Test Anime","genres":[{"name":"Animation"}],"remoteIds":[{"sourceName":"AniDB","id":"123"}]}}


class Response:
    def __init__(self, payload, *, status=200, headers=None):
        self.payload,self.status,self.headers = payload,status,headers or {}
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,limit): return self.payload[:limit]


class IdentityProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"queue.sqlite"
        db = sqlite3.connect(self.path)
        initialize(db)
        db.close()
        self.now = 1000
        self.store = ProviderStore(self.path,clock=lambda:self.now)
        self.memory = MemoryCache(clock=lambda:self.now/1000)
        self.calls,self.responses = [],[]
        def opener(request,timeout):
            self.calls.append(request)
            response = self.responses.pop(0)
            if isinstance(response,Exception): raise response
            return response
        self.client = Clients(self.path,Options("registeredclient",1),opener=opener,store=self.store,memory=self.memory)

    def test_anidb_caches_full_anime_across_instances_and_episode_reads(self):
        self.responses.append(Response(gzip.compress(ANIME),headers={"Content-Encoding":"gzip"}))
        self.assertEqual(self.client.anime(123)["anidb_id"],123)
        self.assertEqual(self.client.episodes(123),{1:987})
        self.assertNotIn(2,self.client.episodes(123))
        other = Clients(self.path,self.client.options,opener=lambda *a,**k:self.fail("cache should avoid network"),store=self.store)
        self.assertEqual(other.episodes(123),{1:987})
        self.assertEqual(len(self.calls),1)
        self.assertIn("client=registeredclient",self.calls[0].full_url)
        self.now += 86400000
        self.responses.append(Response(ANIME))
        self.client.anime(123)
        self.assertEqual(len(self.calls),2)

    def test_anidb_bad_xml_and_errors_apply_shared_backoff_without_cache_poisoning(self):
        self.responses.append(Response(b'<error code="500">private body</error>'))
        with self.assertRaises(ProviderInvalidResponse) as failure: self.client.anime(123)
        self.assertNotIn("private",str(failure.exception))
        self.assertIsNone(self.store.public_get("anidb","anime:123"))
        with self.assertRaises(ProviderDeferred): self.client.anime(124)
        self.assertEqual(len(self.calls),1)

    def test_titles_daily_cache_and_mapping_are_independently_paced(self):
        self.responses.extend((Response(gzip.compress(TITLES)),Response(MAPPING)))
        self.assertEqual(set(self.client.title_candidates(["Test Anime"])),{123})
        self.assertEqual(self.client.mapping(123),456)
        self.assertEqual(set(self.client.title_candidates(["Test Anime"])),{123})
        self.assertEqual(len(self.calls),2)

    def test_tvdb_login_defers_get_then_reuses_token_and_series_cache(self):
        secret = Credentials("private-api-key")
        self.responses.append(Response(b'{"data":{"token":"private-bearer"}}'))
        with self.assertRaises(ProviderDeferred): self.client.series(456,secret)
        self.now += 1000
        self.responses.append(Response(json.dumps(TVDB).encode()))
        value = self.client.series(456,secret)
        self.assertTrue(value["animation"])
        self.assertEqual(self.client.series(456,secret),value)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1].get_header("Authorization"),"Bearer private-bearer")
        self.assertEqual(json.loads(self.calls[0].data),{"apikey":"private-api-key"})
        self.assertNotIn("private",repr(secret))
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM provider_public_cache WHERE provider='tvdb'").fetchone()[0],0)
            dump = '\n'.join(db.iterdump())
            self.assertNotIn("private",dump)
        finally: db.close()

    def test_authentication_failure_has_readable_typed_error(self):
        self.responses.append(urllib.error.HTTPError("https://api4.thetvdb.com/v4/login",401,"private",{},None))
        with self.assertRaises(ProviderAuthenticationFailed) as failure:
            self.client.series(456,Credentials("private-key"))
        self.assertNotIn("private",str(failure.exception))

    def test_retry_after_honored_for_rate_limit(self):
        self.responses.append(urllib.error.HTTPError("http://api.anidb.net:9001/httpapi",429,"",{"Retry-After":"1200"},None))
        with self.assertRaises(ProviderUnavailable): self.client.anime(123)
        with self.assertRaises(ProviderDeferred) as deferred: self.client.anime(123)
        self.assertEqual(deferred.exception.delay_ms,1200000)
        self.assertEqual(retry_after("99999999",self.now),86400000)

    def test_compression_bounds_and_structural_identity_checks(self):
        self.assertEqual(decode(zlib.compress(ANIME),"deflate",4194304),ANIME)
        for payload,codec,limit in ((gzip.compress(b'x'*1000),"gzip",10),(b'bad',"br",100),(b'bad',"gzip",100)):
            with self.assertRaises(ProviderInvalidResponse): decode(payload,codec,limit)
        for data in (ANIME.replace(b'id="123"',b'id="124"',1),b'<!DOCTYPE anime [<!ENTITY x "value">]><anime/>'):
            with self.assertRaises(ProviderInvalidResponse): anime_record(data,123)
        with self.assertRaises(ProviderInvalidResponse): episode_catalog(ANIME.replace(b'987',b'0'),123)
        with self.assertRaises(IdentityConflict): mapping_id(MAPPING.replace(b'</anime-list>',b'<anime anidbid="123" tvdbid="789"/></anime-list>'),123)
        with self.assertRaises(ProviderInvalidResponse): tvdb_record({"data":{"id":999}},456)
        self.assertEqual(title_matches(TITLES, ["No match"]), {})
        self.assertEqual(set(title_matches(TITLES, ["Test Anime"])), {123})


    def test_memory_expiry_clock_rollback_and_authorization_scope(self):
        cache = self.memory
        first,second = Credentials("one"),Credentials("two")
        cache.put(("series",456,first.fingerprint()),{"title":"one"},1)
        self.assertIsNone(cache.get(("series",456,second.fingerprint())))
        item = cache.get(("series",456,first.fingerprint()))
        item["title"]="mutated"
        self.assertEqual(cache.get(("series",456,first.fingerprint())),{"title":"one"})
        self.now += 1000
        self.assertIsNone(cache.get(("series",456,first.fingerprint())))
        cache.put("token","private",100)
        self.now -= 1
        self.assertIsNone(cache.get("token"))

    def test_series_401_invalidates_token_and_next_attempt_authenticates_again(self):
        secret = Credentials("private-key")
        self.memory.put(("token",secret.fingerprint()),"revoked",1800)
        self.responses.append(urllib.error.HTTPError("https://api4.thetvdb.com/v4/series/456/extended",401,"",{},None))
        with self.assertRaises(ProviderUnavailable): self.client.series(456,secret)
        self.assertIsNone(self.memory.get(("token",secret.fingerprint())))
        self.now += 10000
        self.responses.append(Response(b'{"data":{"token":"new-token"}}'))
        with self.assertRaises(ProviderDeferred): self.client.series(456,secret)
        self.assertEqual(self.calls[-1].full_url,"https://api4.thetvdb.com/v4/login")


    def test_official_episode_pages_reuse_auth_scope_cache_and_never_follow_next_urls(self):
        secret=Credentials("private-key")
        self.memory.put(("token",secret.fingerprint()),"private-token",1800)
        document={"data":{"episodes":[{"id":100,"seriesId":456,"seasonNumber":0,"number":1}]},
                  "links":{"next":"https://untrusted.invalid/never-follow"}}
        self.responses.append(Response(json.dumps(document).encode()))
        self.assertEqual(self.client.episode_page(456,0,secret),(((100,0,1),),True))
        self.assertEqual(self.client.episode_page(456,0,secret),(((100,0,1),),True))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0].full_url,"https://api4.thetvdb.com/v4/series/456/episodes/official?page=0")
        from src.anime.providers.parsing import tvdb_episode_page
        document["data"]["episodes"][0]["seriesId"]=999
        with self.assertRaises(ProviderInvalidResponse): tvdb_episode_page(document,456)
