"""Management API: service status for the dashboard bar, and Keep refused while Sonarr cannot work."""
import time
import unittest

from src.anime.storage.sqlite import connect, transaction
from src.anime.tests import test_api_v1 as v1

ADMIN, WEB = v1.ADMIN, v1.WEB


class ApiServiceStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = v1.ApiV1Tests.asyncSetUp
    rows = v1.ApiV1Tests.rows
    request = v1.ApiV1Tests.request
    release = v1.ApiV1Tests.release

    def sonarr(self, status, detail, *, age_ms=0):
        now = time.time_ns() // 1000000
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("INSERT OR REPLACE INTO service_status VALUES('sonarr','Sonarr',?,?,?,?,?)",
                           (status, detail, now, now, now - age_ms))
        finally:
            db.close()

    async def staged(self):
        _, result, _ = await self.request('POST', '/api/v1/webhook', self.release(), token=WEB)
        target = result['data']['job_id']
        db = connect(self.database)
        try:
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (target,))
                db.execute("UPDATE job_operations SET state='STAGED' WHERE operation_id=(SELECT operation_id "
                           "FROM job_operation_members WHERE job_id=?)", (target,))
        finally:
            db.close()
        return target

    async def test_status_bar_lists_every_service_and_needs_the_admin_session(self):
        self.sonarr('DOWN', 'HTTP 503 at http://sonarr:8989')
        status, result, _ = await self.request('GET', '/api/v1/services', token=ADMIN)
        self.assertEqual(status, 200)
        items = {item['service']: item for item in result['data']['services']}
        self.assertEqual(list(items), ['qbittorrent', 'prowlarr', 'sonarr', 'jellyfin', 'shoko',
                                       'anidb', 'anime_lists', 'tvdb'])
        self.assertEqual((items['sonarr']['status'], items['sonarr']['message'], items['sonarr']['stale']),
                         ('DOWN', 'Sonarr is unavailable: HTTP 503 at http://sonarr:8989', False))
        self.assertEqual(items['qbittorrent']['status'], 'UNKNOWN')        # Not written yet.
        self.assertIn('has not checked it yet', items['qbittorrent']['message'])
        self.assertTrue(items['qbittorrent']['required'])
        status, _, _ = await self.request('GET', '/api/v1/services', token=WEB)
        self.assertEqual(status, 401)





if __name__ == '__main__':
    unittest.main()
