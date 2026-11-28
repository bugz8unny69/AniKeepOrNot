"""Tests for Dashboard Running page assets, API and Intake episode views."""
import json
import sqlite3
import unittest

from src.anime.api.app import Application
from src.anime.api.auth import COOKIE
from src.anime.dashboard.assets import load_assets
from src.anime.secrets import SecretStore
from src.anime.series_job.store import wake, add_torrent, get_series, update_series
from src.anime.tests import support as fixture

WEB = "w" * 43
ADMIN = "m" * 43
ORIGIN = "https://anime.example"


class DummyBridge:
    async def run(self): pass
    async def close(self): pass


class DashboardSeriesApiTests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.DatabaseSetup.rows

    async def asyncSetUp(self):
        await fixture.DatabaseSetup.asyncSetUp(self)
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
        content_type = dict(output[0]["headers"]).get(b"content-type", b"").decode()
        if "application/json" in content_type:
            result = json.loads(output[1]["body"])
        else:
            result = output[1]["body"].decode("utf-8", errors="replace")
        return output[0]["status"], result, dict(output[0]["headers"])

    def test_running_route_in_assets(self):
        assets = load_assets()
        self.assertIn("/running", assets)
        self.assertNotIn("/jobs", assets)
        mime, content = assets["/running"]
        self.assertEqual(mime, "text/html; charset=utf-8")
        self.assertIn(b"running-panel", content)
        self.assertNotIn(b"jobs-panel", content)
        self.assertIn(b"[2] running", content)

    async def test_get_running_html_page(self):
        status, content, headers = await self.request("GET", "/running")
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"text/html; charset=utf-8")
        self.assertIn("running-panel", content)

    async def test_running_api_returns_working_series(self):
        db = sqlite3.connect(self.database)
        aid = 16999
        wake(db, aid, "Edgerunners")
        add_torrent(db, aid, {"infohash": "a" * 40, "release_name": "Show - 01", "step": "download", "progress": 0.5})
        series = get_series(db, aid)
        series.torrents[0]["state"] = "working"
        series.episodes["S01E01"] = {"state": "downloading", "torrent": "a" * 40}
        update_series(db, series)
        db.close()

        status, result, _ = await self.request("GET", "/api/v1/running", token=ADMIN)
        self.assertEqual(status, 200)
        series_list = result["data"]["series"]
        self.assertEqual(len(series_list), 1)
        self.assertEqual(series_list[0]["anidb_id"], aid)
        self.assertEqual(series_list[0]["title"], "Edgerunners")
        self.assertEqual(series_list[0]["torrents"][0]["step"], "download")
        self.assertEqual(series_list[0]["torrents"][0]["progress"], 0.5)

    async def test_intake_series_detail_by_episode(self):
        db = sqlite3.connect(self.database)
        aid = 17002
        wake(db, aid, "Intake Show")
        add_torrent(db, aid, {"infohash": "b" * 40, "release_name": "Intake Show - 01"})
        series = get_series(db, aid)
        series.episodes["S01E01"] = {"state": "staged", "torrent": "b" * 40, "file": "Intake Show/S01E01.mkv"}
        update_series(db, series)
        db.close()

        status, result, _ = await self.request("GET", f"/api/v1/intake/series/{aid}", token=ADMIN)
        self.assertEqual(status, 200)
        data = result["data"]
        self.assertEqual(data["anidb_id"], aid)
        self.assertEqual(data["title"], "Intake Show")
        self.assertIn("S01E01", data["episodes"])
        self.assertEqual(data["episodes"]["S01E01"]["state"], "staged")
        self.assertEqual(data["episodes"]["S01E01"]["file"], "Intake Show/S01E01.mkv")
        self.assertEqual(len(data["torrents"]), 1)


class MissingPagesTests(DashboardSeriesApiTests):
    """Owner, 2026-11-27: a dashboard page that is not there (the old Jobs page) opens Intake."""

    async def test_a_page_that_is_not_there_opens_intake_under_every_address(self):
        for path, location in (("/jobs", "/"), ("/akon/jobs", "/akon/"), ("/anime-discovery/jobs", "/anime-discovery/"),
                               ("/no/such/page", "/")):
            with self.subTest(path=path):
                status, _, headers = await self.request("GET", path)
                self.assertEqual(status, 302)
                self.assertEqual(headers[b"location"], location.encode())
        status, _, _ = await self.request("GET", "/")                       # Intake itself is served.
        self.assertEqual(status, 200)
        status, _, _ = await self.request("GET", "/favicon.ico")            # A file that is not there: 404.
        self.assertEqual(status, 404)
        status, body, _ = await self.request("GET", "/api/v1/no-such-route")  # The API keeps its JSON errors.
        self.assertNotEqual(status, 302)
        self.assertIn("error", body)

    async def test_the_dashboard_no_longer_sends_anyone_to_the_jobs_page(self):
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "dashboard.js").read_text()
        self.assertNotIn("location.assign('/jobs", script)


class DashboardLoadsTests(unittest.TestCase):
    """Production, 2026-11-27: the dashboard showed nothing and made no request: at load,
    dashboard.js attached listeners to the removed Jobs page's controls ($('search') was
    null), and the error stopped the whole script. This runs the script at load against a
    page with exactly index.html's element IDs (dashboard_load.js, in Node)."""

    def test_the_script_loads_against_the_page_it_is_served_with(self):
        import shutil, subprocess
        from pathlib import Path
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        here = Path(__file__).resolve().parent
        static = here.parent / "dashboard" / "static"
        for page in ("/", "/running", "/recognized", "/settings"):
            with self.subTest(page=page):
                result = subprocess.run([node, str(here / "dashboard_load.js"), str(static / "dashboard.js"),
                                         str(static / "index.html"), page], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("no error at load", result.stdout)


class WorkloadSettingsTests(DashboardSeriesApiTests):
    """Owner, 2026-11-28: 'Series worked on at once' and 'Torrents worked on at once per series'
    share the Workload panel; its save sends each under its own section in one request."""

    async def test_one_save_sets_both_workload_settings(self):
        status, current, _ = await self.request("GET", "/api/v1/settings", token=ADMIN)
        self.assertEqual(status, 200)
        current = current["data"]
        self.assertIn("series.torrents_at_once", current["fields"])
        body = {"expected_revision": current["desired"]["revision"],
                "settings": {"worker": {"concurrency": 3}, "series": {"torrents_at_once": 2}}}
        status, saved, _ = await self.request("PATCH", "/api/v1/settings", body, token=ADMIN)
        self.assertEqual(status, 200, saved)
        saved = saved["data"]
        self.assertEqual(saved["configuration"]["settings"]["worker"]["concurrency"], 3)
        self.assertEqual(saved["configuration"]["settings"]["series"]["torrents_at_once"], 2)

    def test_the_workload_panel_shows_both(self):
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "dashboard.js").read_text()
        self.assertIn("const PANEL_OF = {'series.torrents_at_once': 'worker'};", script)
        self.assertIn("section === 'worker' ? 'Workload'", script)
        self.assertIn("(values[own] ||= {})[key] = value;", script)      # Saved under its own section.
