"""Exact recognized episode lookup skips providers without skipping downstream gates.

Authored offline regressions; CI execution remains withheld until approved.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from src.anime.providers.releases import make_verifier
from src.anime.release_contracts import ReleaseInput, ReleaseNotReady
from src.anime.storage.sqlite import initialize


class CachedReleaseVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "anime.sqlite")
        db = sqlite3.connect(self.database)
        db.execute("PRAGMA foreign_keys=ON")
        initialize(db)
        db.execute("INSERT INTO identified_anime VALUES(1001,2001,'Example Anime')")
        db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'autobrr',X'7B7D',1)")
        db.execute("INSERT INTO jobs(job_id,event_id,kind,payload_json,available_at_ms,created_at_ms,updated_at_ms) VALUES(1,1,'VERIFY_RELEASE','{}',1,1,1)")
        db.execute("INSERT INTO identity_results VALUES(1,1,1001,2,'{}',1)")
        db.execute("INSERT INTO identified_episodes VALUES(30013,1001)")
        db.execute("INSERT INTO release_verifications VALUES(1,1,1,1001,?,'DECLARED_SINGLE_EPISODE',13,30013,2001,2,1,40001,'official',1000)",('a'*64,))
        db.execute("INSERT INTO recognized_seasons VALUES(2001,2,1001,'Example Anime',1,1000)")
        db.execute("INSERT INTO recognized_episodes VALUES(2001,2,1001,13,30013,1,40001,1,1000)")
        db.commit()
        db.close()

        class Configuration:
            def __init__(self, path):self.path=path
            def value(self, key):
                if key == "storage.database_path":return self.path
                raise AssertionError("Provider config should not be needed for known episode")

        self.configuration = Configuration(self.database)
        def forbidden(*args, **kwargs):
            raise AssertionError("Known episode must not invoke AniDB/TVDB providers")
        self.verify = make_verifier(forbidden, forbidden, client_factory=forbidden)

    async def test_exact_tvdb_season_episode_reuses_proof_without_network(self):
        source = ReleaseInput(1,1,1001,2001,
            json.dumps({"release_name":"Example Anime S02E01"}).encode())
        result = await self.verify(source,self.configuration,threading.Event())
        self.assertEqual((result.anidb_episode_number,result.anidb_episode_id),(13,30013))
        self.assertEqual((result.tvdb_season,result.tvdb_episode,result.tvdb_episode_id),(2,1,40001))

    async def test_unknown_episode_does_not_guess_from_nearby_cache(self):
        source = ReleaseInput(1,1,1001,2001,
            json.dumps({"release_name":"Example Anime S02E02"}).encode())
        # Cache miss must fall through to provider resolver, rather than
        # return a fabricated mapping based on adjacent numbering.
        with self.assertRaisesRegex(AssertionError,"providers"):
            await self.verify(source,self.configuration,threading.Event())

    async def test_cancelled_request_does_not_use_cached_placement(self):
        source = ReleaseInput(1,1,1001,2001,
            json.dumps({"release_name":"Example Anime S02E01"}).encode())
        cancelled=threading.Event()
        cancelled.set()
        with self.assertRaises(ReleaseNotReady):
            await self.verify(source,self.configuration,cancelled)


if __name__=="__main__":
    unittest.main()
