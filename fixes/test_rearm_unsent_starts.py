"""No CI required; offline test of letting stuck unsent starts go out once."""
import importlib.util
from pathlib import Path
import unittest

from src.anime.storage.transfers import TransferJournal
from src.anime.tests import test_torrent_transfer as transfers

spec = importlib.util.spec_from_file_location('rearm_unsent_starts',
                                              Path(__file__).with_name('rearm_unsent_starts.py'))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class OldJournal(TransferJournal):
    """The code before the fix: an unsent start stays START_UNCERTAIN."""
    def start_not_sent(self, claim, plan_id):
        return False


class RearmUnsentStartsTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the download fixture without re-running its tests.
    Base = transfers.TorrentTransferTests
    asyncSetUp, rows, source, worker, queued, due = (
        Base.asyncSetUp, Base.rows, Base.source, Base.worker, Base.queued, Base.due)
    del Base

    def job(self):
        return self.rows("SELECT job_id FROM jobs WHERE kind='DOWNLOAD_TORRENT'")[0][0]

    async def test_stuck_unsent_start_is_rearmed_and_sent_once(self):
        worker = self.worker(mode='start_snapshot_changed', journal_factory=OldJournal)
        await self.queued(worker)
        await worker.attempt()
        for _ in range(2):
            self.due(); await worker.attempt()
        self.assertEqual((self.starts, self.rows('SELECT phase FROM torrent_transfers')),
                         (0, [('START_UNCERTAIN',)]))                 # Stuck, as on production.

        preview = fix.execute(self.database, [self.job()])
        self.assertEqual(preview['jobs'][0]['status'], 'rearm')
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'), [('START_UNCERTAIN',)])

        applied = fix.execute(self.database, [self.job()], apply=True)
        self.assertEqual(applied['rearmed'], [self.job()])
        self.assertIn('.before-rearm-start-', applied['backup'])
        self.due(); await worker.attempt()
        self.assertEqual((self.starts, self.rows('SELECT phase FROM torrent_transfers')), (1, [('ACTIVE',)]))

        again = fix.execute(self.database, [self.job()], apply=True)
        self.assertEqual((again['rearmed'], again['jobs'][0]['status']), ([], 'not_stuck'))

    async def test_a_start_that_may_have_been_sent_is_left_alone(self):
        worker = self.worker(mode='start_timeout_absent')                # Sent, then the reply was lost.
        await self.queued(worker)
        await worker.attempt()
        self.due(); await worker.attempt()
        report = fix.execute(self.database, [self.job()], apply=True)
        self.assertEqual((report['jobs'][0]['status'], report['rearmed'], report['backup']),
                         ('start_may_have_been_sent', [], None))
        self.assertEqual(self.rows('SELECT phase FROM torrent_transfers'), [('START_UNCERTAIN',)])


if __name__ == '__main__':
    unittest.main()
