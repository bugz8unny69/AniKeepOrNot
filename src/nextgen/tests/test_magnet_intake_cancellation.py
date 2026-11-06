"""Pre-manifest intents must block unsafe resource release before cleanup exists."""
import sqlite3
import unittest

from src.nextgen.storage.cancellation import unfinished_resources


class PreManifestCancellationTests(unittest.TestCase):
    def test_premetainfo_intent_counts_as_live_resource(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE acquisition_plans(job_id INTEGER);
            CREATE TABLE staging_cleanup_artifacts(operation_id INTEGER);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE sonarr_magnet_intents(job_id INTEGER,state TEXT);
            CREATE TABLE batch_metadata_admissions(verification_job_id INTEGER,state TEXT);
            INSERT INTO job_operation_members VALUES(21,10);
        """)
        self.assertFalse(unfinished_resources(db,10))
        db.execute("INSERT INTO sonarr_magnet_intents VALUES(21,'UNCERTAIN')")
        self.assertTrue(unfinished_resources(db,10))
        self.assertFalse(unfinished_resources(db,11))
