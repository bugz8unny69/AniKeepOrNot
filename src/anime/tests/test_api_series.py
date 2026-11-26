"""Tests for series actions and running endpoints in the API."""
import json
import sqlite3
import unittest

from src.anime.api.app import Application
from src.anime.api.auth import COOKIE
from src.anime.secrets import SecretStore
from src.anime.series_job.store import wake, add_torrent
from src.anime.tests import test_acquisition_preparation as fixture

WEB = "w" * 43
ADMIN = "m" * 43
ORIGIN = "https://anime.example"


class DummyBridge:
    async def run(self): pass
    async def close(self): pass


class ApiSeriesTests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.AcquisitionPreparationTests.rows

    async def asyncSetUp(self):
        await fixture.AcquisitionPreparationTests.asyncSetUp(self)
        self.secrets = SecretStore(self.root)
        self.secrets.initialize(webhook_token=WEB)
        self.app = Application(self.root, origin=ORIGIN)
        self.app.bridge = DummyBridge()
        self.app.passwords.replace(ADMIN, expected_revision=0)
        session, csrf = self.app.sessions.create(self.app.passwords.read()[1])
        self.admin_cookie = COOKIE + "=" + session
        self.admin_csrf = csrf
        self.addCleanup(self.app.close)

    async def request(self, method, path, body=None, *, token=None, headers=None):
        route, _, query = path.partition("?")
        raw = b"" if body is None else json.dumps(body).encode()
        base = [(b"host", b"anime.example"), (b"content-length", str(len(raw)).encode())]
        if raw:
            base.append((b"content-type", b"application/json"))
        if token == ADMIN:
            session_headers = {"cookie": self.admin_cookie}
            if method != "GET":
                session_headers.update({"origin": ORIGIN, "x-csrf-token": self.admin_csrf})
            headers = {**session_headers, **(headers or {})}
            token = None
        if token is not None:
            base.append((b"authorization", ("Bearer " + token).encode()))
        base.extend((k.lower().encode(), v.encode()) for k, v in (headers or {}).items())
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": route,
            "raw_path": route.encode(),
            "query_string": query.encode(),
            "headers": base,
            "client": ("127.0.0.1", 123),
        }

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        output = []

        async def send(message):
            output.append(message)

        await self.app(scope, receive, send)
        result = json.loads(output[1]["body"])
        return output[0]["status"], result, dict(output[0]["headers"])

    async def test_running_endpoint(self):
        db = sqlite3.connect(self.database)
        wake(db, 16999, "Cyberpunk Edgerunners")
        db.close()

        status, result, _ = await self.request("GET", "/api/v1/running", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertIn("series", result["data"])
        self.assertIn("waiting_identity", result["data"])
        self.assertEqual(len(result["data"]["series"]), 1)
        self.assertEqual(result["data"]["series"][0]["anidb_id"], 16999)

        for prefix in ("/akon", "/anime-discovery"):
            st, res, _ = await self.request("GET", f"{prefix}/api/v1/running", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(res["data"]["series"][0]["anidb_id"], 16999)

    async def test_series_get_and_actions(self):
        aid = 17001
        db = sqlite3.connect(self.database)
        wake(db, aid, "Test Series")
        add_torrent(db, aid, {"infohash": "a" * 40, "release_name": "Test Release"})
        db.close()

        # GET series
        status, res, _ = await self.request("GET", f"/api/v1/series/{aid}", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["title"], "Test Series")
        self.assertEqual(len(res["data"]["torrents"]), 1)

        # POST retry
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/retry", token=ADMIN)
        self.assertEqual(status, 200)

        # POST cancel
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/cancel", token=ADMIN)
        self.assertEqual(status, 200)

        # POST search
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/search", token=ADMIN)
        self.assertEqual(status, 200)

        # POST keep
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/keep", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(res["data"]["state"], "KEPT")

        # Second keep is safe
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/keep", token=ADMIN)
        self.assertEqual(status, 200)

        # Rejecting a kept series returns 409
        status, res, _ = await self.request("POST", f"/api/v1/series/{aid}/reject", token=ADMIN)
        self.assertEqual(status, 409)

    async def test_series_not_found(self):
        status, _, _ = await self.request("GET", "/api/v1/series/99999", token=ADMIN)
        self.assertEqual(status, 404)

        status, _, _ = await self.request("POST", "/api/v1/series/99999/keep", token=ADMIN)
        self.assertEqual(status, 404)
