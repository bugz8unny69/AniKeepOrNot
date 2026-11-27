"""Owner, 2026-11-27: a storage folder that is a mount must be mounted before AKON uses it."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.anime.series_job.steps.common import SERVICE_WAIT_MS, outage
from src.anime.storage_mounts import FACTS, StorageNotMounted, check, learned, mounts

ROOT_FS = "1 0 0:1 / / rw - ext4 /dev/sda1 rw\n"


def line(mount_point, source, fstype="nfs4"):
    escape = lambda text: text.replace(" ", chr(92) + "040")
    return f"9 1 0:9 / {escape(mount_point)} rw - {fstype} {escape(source)} rw\n"


class StorageMountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.database = str(self.base / "akon.sqlite")
        self.media = self.base / "media"
        self.staging = self.media / "Anime Trial"
        self.staging.mkdir(parents=True)
        self.root = str(self.staging)

    def check(self, text):
        return check(self.database, self.root, now_ms=1, text=ROOT_FS + text)

    def test_a_local_folder_stays_simple(self):
        self.assertEqual(self.check(""), "local")
        self.assertEqual(learned(self.database), {})
        self.assertFalse((self.base / FACTS).exists())

    def test_a_mount_is_learned_then_required(self):
        own = line(self.root, "nas:/store/media/Anime Trial")
        self.assertEqual(self.check(own), "learned")
        self.assertEqual(learned(self.database)[self.root]["source"], "nas:/store/media/Anime Trial")
        self.assertEqual(self.check(own), "mounted")
        # Today's failure: only the share above is mounted, the folder is a plain directory on it.
        with self.assertRaises(StorageNotMounted) as caught:
            self.check(line(str(self.media), "nas:/store/media"))
        self.assertIn("not mounted", str(caught.exception))
        self.assertIn(FACTS, str(caught.exception))              # It says how to undo a deliberate change.
        with self.assertRaises(StorageNotMounted):
            self.check("")                                        # Nothing mounted at all: the local disk.

    def test_a_network_share_seen_from_above_learns_the_deeper_mount_and_never_goes_back(self):
        above = line(str(self.media), "nas:/store/media")
        self.assertEqual(self.check(above), "learned")            # On a network filesystem: remembered.
        self.assertEqual(self.check(above + line(self.root, "nas:/store/media/Anime Trial")), "learned")
        self.assertEqual(learned(self.database)[self.root]["mount_point"], self.root)
        with self.assertRaises(StorageNotMounted):
            self.check(above)

    def test_a_remount_is_the_same_mount(self):
        own = line(self.root, "nas:/store/media/Anime Trial")
        self.check(own)
        # A new device number changes nothing: the mount is known by path and source only.
        self.assertEqual(self.check(own.replace("0:9", "0:45")), "mounted")

    def test_a_learned_mount_whose_folder_is_missing_is_not_mounted(self):
        self.check(line(self.root, "nas:/store/media/Anime Trial"))
        self.staging.rmdir()
        with self.assertRaises(StorageNotMounted):
            self.check("")

    def test_mountinfo_escapes_are_read(self):
        self.assertIn(("/mnt/Anime Trial", "nas:/x", "nfs4"), mounts(line("/mnt/Anime Trial", "nas:/x")))

    def test_not_mounted_is_an_outage(self):
        exc = StorageNotMounted(self.root, {"mount_point": self.root, "source": "nas:/x"}, ("/", "/dev/sda1"))
        self.assertEqual(outage(exc), "STORAGE_UNAVAILABLE")


class SeriesWaitsForTheMountTests(unittest.TestCase):
    """Staging and Reject with the staging mount missing: the series waits; nothing is dropped or removed."""

    def setUp(self):
        from src.anime.storage.sqlite import initialize
        from src.anime.series_job.store import wake
        self.db = sqlite3.connect(":memory:")
        initialize(self.db)
        wake(self.db, 7, "Mounted Show", now=1000)
        self.missing = StorageNotMounted("/mnt/media/Anime-Trial", {"mount_point": "/mnt/media/Anime-Trial",
                                         "source": "nas:/x"}, ("/mnt/media", "nas:/store/media"))

    def tearDown(self):
        self.db.close()

    def context(self):
        from src.anime.tests.test_series_resilience import SeriesResilienceTests
        helper = SeriesResilienceTests("test_a_timeout_in_a_step_waits")
        helper.db = self.db
        helper.addCleanup = self.addCleanup
        return helper.context()

    def test_staging_waits_for_the_mount(self):
        from src.anime.series_job.store import add_torrent, get_series
        from src.anime.series_job.worker import run_series_attempt
        add_torrent(self.db, 7, {"infohash": "7" * 40, "release_name": "[G] Show - 01", "step": "stage",
                                 "state": "working", "episodes": ["S01E01"], "files": [], "qb": {"marker": "a" * 32}},
                    now=1000)
        with patch("src.anime.storage_mounts.require", side_effect=self.missing), \
                self.assertLogs("akon.series", level="WARNING"):
            asyncio.run(run_series_attempt(self.db, self.context(), now=5000))
        torrent = get_series(self.db, 7).torrents[0]
        self.assertEqual((torrent["step"], torrent["state"]), ("stage", "working"))      # Not dropped.
        self.assertEqual(self.db.execute("SELECT state,wake_at_ms FROM series").fetchone(),
                         ("ASLEEP", 5000 + SERVICE_WAIT_MS))

    def test_reject_waits_for_the_mount(self):
        from src.anime.series_job.store import get_series, update_series
        from src.anime.series_job.worker import run_series_attempt
        series = get_series(self.db, 7)
        series.work["reject"] = {}
        update_series(self.db, series, now=1000)
        with patch("src.anime.storage_mounts.require", side_effect=self.missing), \
                self.assertLogs("akon.series", level="WARNING"):
            asyncio.run(run_series_attempt(self.db, self.context(), now=5000))
        series = get_series(self.db, 7)
        self.assertNotEqual(series.state, "REJECTED")
        self.assertEqual((series.state, series.wake_at_ms), ("ASLEEP", 5000 + SERVICE_WAIT_MS))
        self.assertNotIn("stopped", series.work)


class StepsDeclareTheirStorageTests(unittest.TestCase):
    def test_every_step_that_reads_or_writes_storage_is_guarded(self):
        from src.anime.series_job.keep import run_keep
        from src.anime.series_job.reject import run_reject
        from src.anime.series_job.worker import STEP_HANDLERS
        from src.anime.storage_mounts import DOWNLOADS, LIBRARY, STAGING
        declared = {name: getattr(step, "storage", ()) for name, step in STEP_HANDLERS.items()}
        self.assertEqual(declared, {
            "get_metadata": (), "check_release": (), "reserve_episodes": (), "download": (),   # qBittorrent's side.
            "check_media": (DOWNLOADS,), "transcode": (STAGING, DOWNLOADS), "stage": (STAGING, DOWNLOADS)})
        self.assertEqual(run_reject.storage, (STAGING, DOWNLOADS))
        self.assertEqual(run_keep.storage, (STAGING, LIBRARY))
