"""No CI required; offline test of the one-time staged-torrent cleanup."""
import importlib.util
from pathlib import Path
import unittest

from unittest.mock import patch

from src.anime.decision_download_cleanup import KIND
from src.anime.tests.test_decision_download_cleanup import DecisionDownloadCleanupTests as Base

spec = importlib.util.spec_from_file_location('remove_staged_torrents',
                                              Path(__file__).with_name('remove_staged_torrents.py'))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class RemoveStagedTorrentsTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the decision-cleanup fixtures without re-running their tests.
    asyncSetUp, rows, source, queued, due, ready = (Base.asyncSetUp, Base.rows, Base.source,
                                                    Base.queued, Base.due, Base.ready)
    staged_ready, prepared, make_due, staged, attach = (Base.staged_ready, Base.prepared,
                                                        Base.make_due, Base.staged, Base.attach)

    async def old_staged_episode(self):
        """A video staged before #204: no staging cleanup was queued."""
        with patch('src.anime.decision_download_cleanup.enqueue_after_staging'):
            return await self.staged()

    async def test_preview_then_apply_queues_one_removal_per_old_staged_episode(self):
        worker, staging, stage_job = await self.old_staged_episode()
        preview = fix.execute(self.database)
        self.assertEqual((preview['eligible_staging_jobs'], preview['queued']), ([stage_job], 0))
        self.assertEqual(self.rows('SELECT count(*) FROM jobs WHERE kind=?', (KIND,)), [(0,)])
        applied = fix.execute(self.database, apply=True)
        self.assertEqual(applied['queued'], 1)
        self.assertTrue(Path(applied['backup']).is_file())
        self.assertEqual(fix.execute(self.database, apply=True)['queued'], 0)   # Second run: nothing.
        # The worker removes the torrent and leftovers; the staged video stays.
        self.payload_root = Path(self.snapshot.observation.save_path)
        self.remainder = self.payload_root / self.snapshot.observation.files[0].path
        self.remainder.parent.mkdir(parents=True, exist_ok=True)
        self.remainder.write_bytes(b'partial')
        self.attach(worker)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE kind=?', (KIND,)), [('DONE',)])
        self.assertEqual(self.controls, ['stop', 'remove'])
        self.assertFalse(self.remainder.exists())
        self.assertEqual(len(list(staging.rglob('*.mkv'))), 1)

    async def test_decided_episodes_are_left_alone(self):
        from src.anime.storage.management_actions import ManagementActions
        worker, _, stage_job = await self.old_staged_episode()
        ManagementActions(self.database).submit(stage_job, 'reject')
        report = fix.execute(self.database)
        self.assertEqual((report['eligible_staging_jobs'], report['skipped']), ([], 1))


if __name__ == '__main__':
    unittest.main()
