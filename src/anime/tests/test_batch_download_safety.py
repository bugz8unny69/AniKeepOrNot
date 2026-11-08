"""Offline pinned-root and cleanup-gate protection checks."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.anime.batch_decision_cleanup import (
    BatchDecisionCleanup, protected_staging_roots,
)
from src.anime.cancellation import CancellationError, CleanupBusy
from src.anime.jobs.cleanup_batch_download_job import registration


class BatchDownloadSafetyTests(unittest.TestCase):
    def test_pinned_staging_roots_are_all_protected_across_configuration_changes(self):
        publications=(
            (10,1,json.dumps({'root':'/old/staging','transfer':'COPY'})),
            (10,2,json.dumps({'root':'/new/staging','transfer':'COPY'})),
            (10,3,json.dumps({'root':'/old/staging','transfer':'COPY'})),
        )
        self.assertEqual(protected_staging_roots(publications),
                         ('/new/staging','/old/staging'))

    def test_missing_or_non_copy_staging_origin_blocks_cleanup(self):
        for rows in (
            (), ((10,1,'not json'),),
            ((10,1,json.dumps({'root':'/stage','transfer':'MOVE'})),),
            ((10,1,json.dumps({'transfer':'COPY'})),),
        ):
            with self.subTest(rows=rows),self.assertRaises(CancellationError):
                protected_staging_roots(rows)

    def test_cancelled_attempt_is_fenced_before_database_mutation(self):
        service=BatchDecisionCleanup.__new__(BatchDecisionCleanup)
        service.cancelled=threading.Event()
        service.cancelled.set()
        with self.assertRaises(CancellationError):
            service._fence()


class BatchDownloadBusyTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_lock_contention_defers_instead_of_failing_cleanup(self):
        class Service:
            def __init__(self,configuration,secrets):
                pass
            def run(self,claim,decision,cancelled):
                raise CleanupBusy()
        context=SimpleNamespace(
            data=lambda:{'decision_job_id':123},
            claim=SimpleNamespace(job_id=124),
            cancelled=threading.Event(),
            configuration=object(),
        )
        result=await registration(object(),service_factory=Service).execute(
            context)
        self.assertEqual((result.kind,result.reason_code,result.delay_ms),
                         ('defer','DECISION_DOWNLOAD_CLEANUP_WAITING',1000))

class StoredInventoryTests(unittest.TestCase):
    def test_canonical_record_roundtrips_without_external_metainfo_reader(self):
        import sqlite3
        from src.anime.storage.batch_submission_journal import stored_inventory
        from src.anime.acquisition.metadata import BatchInventory, TorrentFile
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE batch_submission_inventories(plan_id INTEGER,inventory_json TEXT)')
        digest='a'*40
        sha='b'*64
        inventory=BatchInventory(digest,sha,(TorrentFile(0,'Show/E01.mkv',100),))
        db.execute('INSERT INTO batch_submission_inventories VALUES(?,?)',
                   (17,inventory.json()))
        self.assertEqual(stored_inventory(db,17,digest,sha),inventory)
        with self.assertRaises(ValueError):
            stored_inventory(db,18,digest,sha)
        db.execute('UPDATE batch_submission_inventories SET inventory_json=?',
                   ('{"files":[{"index":0,"path":"Show/E01.mkv","size":100}],'
                    '"files":[]}',))
        with self.assertRaises(ValueError):
            stored_inventory(db,17,digest,sha)
