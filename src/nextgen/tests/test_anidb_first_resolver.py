"""Offline resolver ordering/evidence/cancellation tests; intentionally unrun."""
from dataclasses import replace
import threading
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace

from src.nextgen.configuration_schema import ConfigurationSnapshot, for_operation
from src.nextgen.jobs.identify_anime_job import IdentityRequest, registration
from src.nextgen.providers.errors import ProviderAuthenticationFailed, ProviderInvalidResponse
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.providers.identity import make_resolver
from src.nextgen.storage.sqlite import IdentityConflict, IdentityNotReady


class AniDBFirstResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database=Path(temporary.name)/'identity.sqlite'
        from src.nextgen.storage.sqlite import connect, initialize
        db=connect(database)
        initialize(db)
        db.close()
        startup = ConfigurationSnapshot(1,{'storage':{'database_path':str(database)}})
        self.configuration = for_operation(startup,startup,component="worker")
        self.cancelled = threading.Event()
        self.request = IdentityRequest(1,"normalized-test",b'{"release_name":"[Group] Test Anime - 01 [1080p]"}')
        self.calls = []
        owner = self
        class Client:
            def title_candidates(self,aliases):
                owner.calls.append("anidb_titles")
                return {123:("Test Anime",)}
            def anime(self,aid):
                owner.calls.append("anidb_http")
                return {"anidb_id":aid,"title":"Test Anime","titles":("Test Anime",)}
            def mapping(self,aid):
                owner.calls.append("mapping")
                return 456
            def season_entry(self,tid,season,episode=None):
                owner.calls.append("season_entry")
                raise IdentityNotReady()  # anime-lists has no entry for that season
            def series(self,tid,credentials):
                owner.calls.append("tvdb")
                return {"tvdb_series_id":tid,"titles":("Test Anime",),"animation":True,"live":False,"anidb_ids":frozenset({123})}
        self.client = Client()
        self.resolve = make_resolver(Options("registeredclient",1),lambda:Credentials("private-key"),
                                     client_factory=lambda *args:self.client)

    async def test_anidb_first_then_mapping_and_tvdb_yield_series_only(self):
        value = await self.resolve(self.request,self.configuration,self.cancelled)
        self.assertEqual(self.calls,["anidb_titles","anidb_http","mapping","tvdb"])
        self.assertEqual(value["anime"],{"anidb_id":123,"tvdb_series_id":456,"title":"Test Anime"})
        self.assertEqual(value["episodes"],[])
        self.assertEqual(value["attributes"],{})

    async def test_ambiguous_anidb_match_never_falls_back_to_tvdb_guess(self):
        self.client.title_candidates = lambda aliases:{123:("Test Anime",),789:("Test Anime",)}
        with self.assertRaises(IdentityNotReady):
            await self.resolve(self.request,self.configuration,self.cancelled)
        self.assertNotIn("tvdb",self.calls)

    async def test_conflicting_explicit_remote_id_or_title_stops_identity(self):
        for change in ({"anidb_ids":frozenset({789})},{"anidb_ids":frozenset(),"titles":("Unrelated",)},{"live":True}):
            def series(*args):
                return {"tvdb_series_id":456,"titles":("Test Anime",),"animation":True,"live":False,"anidb_ids":frozenset({123}),**change}
            self.client.series = series
            with self.assertRaises(IdentityConflict):
                await self.resolve(self.request,self.configuration,self.cancelled)

    async def test_verified_id_link_allows_different_title_language(self):
        # W-13: verified provider ID relationship succeeds despite catalog title differences.
        def series(*args):
            return {"tvdb_series_id":456,"titles":("Different Display Title",),"animation":True,"live":False,"anidb_ids":frozenset({123})}
        self.client.series = series
        value = await self.resolve(self.request,self.configuration,self.cancelled)
        self.assertEqual(value["anime"]["anidb_id"], 123)
        self.assertEqual(value["anime"]["tvdb_series_id"], 456)

    async def test_explicit_later_season_and_cancellation_do_not_approve_first_entry(self):
        # S02 never inherits the Season 1 entry: without an anime-lists season entry it stops.
        request = replace(self.request,raw_body=b'{"release_name":"Test Anime S02E01"}')
        with self.assertRaises(IdentityNotReady): await self.resolve(request,self.configuration,self.cancelled)
        self.assertEqual(self.calls,["anidb_titles","anidb_http","mapping","season_entry"])
        self.calls.clear()
        self.cancelled.set()
        with self.assertRaises(IdentityNotReady): await self.resolve(self.request,self.configuration,self.cancelled)
        self.assertEqual(self.calls,[])

    async def test_input_duplicate_keys_and_invalid_credentials_are_not_trusted(self):
        request = replace(self.request,raw_body=b'{"release_name":"First","release_name":"Other"}')
        with self.assertRaises(IdentityNotReady): await self.resolve(request,self.configuration,self.cancelled)
        resolver = make_resolver(Options("registeredclient",1),lambda:None,client_factory=lambda *args:self.client)
        with self.assertRaises(ProviderAuthenticationFailed): await resolver(self.request,self.configuration,self.cancelled)

    async def test_job_adapter_maps_auth_and_invalid_response_to_registered_reasons(self):
        for error,kind,reason in ((ProviderAuthenticationFailed(),"review","PROVIDER_AUTH_FAILED"),
                                  (ProviderInvalidResponse(),"retry","PROVIDER_RESPONSE_INVALID")):
            async def resolve(*args): raise error
            spec = registration(resolve)
            context = SimpleNamespace(data=lambda:{},claim=SimpleNamespace(event_id=1),configuration=self.configuration,cancelled=self.cancelled)
            from unittest.mock import patch
            with patch("src.nextgen.jobs.identify_anime_job._request",return_value=self.request):
                result = await spec.execute(context)
            self.assertEqual((result.kind,result.reason_code),(kind,reason))
