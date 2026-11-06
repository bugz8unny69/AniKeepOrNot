"""Offline v64 -> v65: per-service codes retired into generic service codes; history keeps the service."""
import json
import sqlite3
import unittest

from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure
from src.nextgen.storage.sqlite import initialize

SCRIPT_65 = (SQL_DIR / '065_retired_service_codes.sql').read_text()


class RetiredServiceCodesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:64]:
            self.db.executescript((SQL_DIR / name).read_text())
        self.db.executescript("""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'manual-release',X'7b7d',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms,last_reason_code) VALUES
              (1,1,'REFRESH_JELLYFIN','{}','RETRY_WAIT',0,0,0,'JELLYFIN_UNAVAILABLE'),
              (2,1,'DISCOVER_EPISODE_RELEASES','{"expected_reason":"PROWLARR_AUTH_FAILED"}','NEEDS_REVIEW',0,0,0,'PROWLARR_AUTH_FAILED'),
              (3,1,'IDENTIFY_ANIME','{}','NEEDS_REVIEW',0,0,0,'PROVIDER_AUTH_FAILED'),
              (4,1,'SUBMIT_TORRENT','{}','RETRY_WAIT',0,0,0,'SERVICE_UNAVAILABLE');
            INSERT INTO job_history(job_id,occurred_at_ms,event_code,reason_code,details_json) VALUES
              (1,1,'RETRY_SCHEDULED','JELLYFIN_UNAVAILABLE',NULL),
              (2,2,'NEEDS_REVIEW','PROWLARR_AUTH_FAILED','{"attempt":1}'),
              (3,3,'NEEDS_REVIEW','PROVIDER_AUTH_FAILED','{"attempt":1}'),
              (4,4,'DEFERRED','SERVICE_UNAVAILABLE','{"service":"qbittorrent","cause_reason_code":"DOWNLOAD_CLIENT_UNAVAILABLE"}'),
              (1,5,'NEEDS_REVIEW','JOB_RETRY_EXHAUSTED','{"cause_reason_code":"SHOKO_UNAVAILABLE"}'),
              (2,6,'NEEDS_REVIEW','DOWNLOAD_CLIENT_UNSUPPORTED','{"service":"qbittorrent","service_status":"UNSUPPORTED"}');
        """)

    def migrate(self):
        self.db.executescript(SCRIPT_65)

    def history(self):
        return [(reason, json.loads(details) if details else None) for reason, details in self.db.execute(
            'SELECT reason_code,details_json FROM job_history ORDER BY history_id')]

    def test_codes_become_generic_and_history_names_the_service(self):
        self.migrate()
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 65)
        self.assertEqual(self.history(), [
            ('SERVICE_UNAVAILABLE', {'service': 'jellyfin'}),
            ('SERVICE_SETUP_INVALID', {'attempt': 1, 'service': 'prowlarr', 'service_status': 'AUTH_FAILED'}),
            # The old provider codes did not say which provider: no service is invented.
            ('SERVICE_SETUP_INVALID', {'attempt': 1, 'service_status': 'AUTH_FAILED'}),
            # A wait already named its service; its redundant cause is dropped.
            ('SERVICE_UNAVAILABLE', {'service': 'qbittorrent'}),
            # A retired code that was only the cause still names its service.
            ('JOB_RETRY_EXHAUSTED', {'cause_reason_code': 'SERVICE_UNAVAILABLE', 'service': 'shoko'}),
            # Recorded details are kept as they are.
            ('SERVICE_SETUP_INVALID', {'service': 'qbittorrent', 'service_status': 'UNSUPPORTED'}),
        ])
        self.assertEqual(self.db.execute('SELECT job_id,last_reason_code FROM jobs ORDER BY job_id').fetchall(),
                         [(1, 'SERVICE_UNAVAILABLE'), (2, 'SERVICE_SETUP_INVALID'),
                          (3, 'SERVICE_SETUP_INVALID'), (4, 'SERVICE_UNAVAILABLE')])
        self.assertEqual(json.loads(self.db.execute('SELECT payload_json FROM jobs WHERE job_id=2').fetchone()[0]),
                         {'expected_reason': 'SERVICE_SETUP_INVALID'})

    def test_migrated_history_details_are_valid_for_the_new_code(self):
        from src.nextgen.reasons import safe_details
        self.migrate()
        for _, details in self.history():
            if details:
                with self.subTest(details=details):
                    safe_details(**{k: v for k, v in details.items()})

    def test_guard_rolls_back_if_version_is_not_64(self):
        self.db.execute('PRAGMA user_version=63')
        with self.assertRaises(sqlite3.IntegrityError):
            self.migrate()
        self.assertEqual(self.db.execute('SELECT last_reason_code FROM jobs WHERE job_id=1').fetchone(),
                         ('JELLYFIN_UNAVAILABLE',))

    def test_fresh_schema_matches_sequential_upgrade(self):
        self.migrate()
        for name in SCRIPTS[65:]:  # Later versions build on v65.
            self.db.executescript((SQL_DIR / name).read_text())
        fresh = sqlite3.connect(':memory:')
        try:
            initialize(fresh)
            self.assertEqual(_structure(self.db), _structure(fresh))
        finally:
            fresh.close()


if __name__ == '__main__':
    unittest.main()
