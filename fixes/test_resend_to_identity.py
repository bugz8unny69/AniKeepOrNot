"""No CI required; offline test of sending stopped releases to identification again."""
import importlib.util
from pathlib import Path
import unittest

from src.anime.release_contracts import ReleaseNotReady
from src.anime.tests import test_release_verification as release_checks

spec = importlib.util.spec_from_file_location('resend_to_identity',
                                              Path(__file__).with_name('resend_to_identity.py'))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class ResendToIdentityTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the release-check fixture without re-running its tests.
    Base = release_checks.ReleaseJobTests
    asyncSetUp, rows, worker = Base.asyncSetUp, Base.rows, Base.worker
    del Base

    async def stopped(self, reason='RELEASE_EPISODE_MAPPING_MISSING'):
        async def verify(*args): raise ReleaseNotReady(reason)
        await self.worker(verify).attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs'), [('NEEDS_REVIEW', reason)])

    async def test_preview_then_apply_cancels_and_queues_a_fresh_identification(self):
        await self.stopped()
        preview = fix.execute(self.database, [self.job])
        self.assertEqual(preview['jobs'][0]['status'], 'cancel_and_send')
        self.assertEqual(preview['jobs'][0]['release'], 'Test - 01')
        self.assertEqual(self.rows('SELECT count(*) FROM incoming_events'), [(1,)])   # Preview changes nothing.

        applied = fix.execute(self.database, [self.job], apply=True)
        self.assertEqual((applied['sent'], applied['refused']), ([self.job], []))
        self.assertTrue(Path(applied['backup']).is_file())
        self.assertIn('.before-resend-', applied['backup'])
        item = applied['jobs'][0]
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?', (self.job,)), [('CANCELED',)])
        # Same stored delivery, new event, fresh identification with no AniDB ID.
        self.assertEqual(self.rows('SELECT source,raw_body FROM incoming_events WHERE event_id=?', (item['new_event_id'],)),
                         self.rows('SELECT source,raw_body FROM incoming_events WHERE event_id=?', (self.event,)))
        self.assertEqual(self.rows('SELECT kind,state,payload_json,event_id FROM jobs WHERE job_id=?', (item['identify_job_id'],)),
                         [('IDENTIFY_ANIME', 'PENDING', '{}', item['new_event_id'])])

        again = fix.execute(self.database, [self.job], apply=True)   # Second run: nothing.
        self.assertEqual((again['sent'], again['jobs'][0]['status']), ([], 'already_sent'))
        self.assertEqual(self.rows('SELECT count(*) FROM incoming_events'), [(2,)])

    async def test_other_review_reasons_are_left_alone(self):
        await self.stopped('RELEASE_UNRESOLVED')
        report = fix.execute(self.database, [self.job], apply=True)
        self.assertEqual((report['jobs'][0]['status'], report['sent'], report['backup']), ('not_eligible', [], None))
        self.assertEqual(self.rows('SELECT state FROM jobs'), [('NEEDS_REVIEW',)])


if __name__ == '__main__':
    unittest.main()
