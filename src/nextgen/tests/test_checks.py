"""Checks log what they checked and what failed at DEBUG (JD-1d)."""
import asyncio
import io
import logging
import unittest
from unittest import mock

from src.nextgen import checks
from src.nextgen.checks import check, here, require


def logger(level):
    stream = io.StringIO()
    target = logging.Logger('nextgen.worker.job.test')
    handler = logging.StreamHandler(stream)
    target.addHandler(handler)
    target.setLevel(level)
    return target, stream


class Stop(Exception):
    def __init__(self, failed_check=None, failed_path=None):
        super().__init__()
        self.failed_check, self.failed_path = failed_check, failed_path


class CheckTests(unittest.TestCase):
    def test_a_check_logs_the_real_line_result_expected_actual_and_path_at_debug(self):
        log, out = logger(logging.DEBUG)
        self.assertFalse(check(log, 'EPISODE_SIZE', False, expected=100, actual=250, path='/d/a.mkv', job_id=7))
        line = out.getvalue()
        self.assertRegex(line, r'^job #7 · check EPISODE_SIZE failed · tests/test_checks\.py:\d+ · 250 \(expected 100\) · /d/a\.mkv$')
        self.assertTrue(check(log, 'EPISODE_SIZE', True))
        self.assertIn('check EPISODE_SIZE passed', out.getvalue())

    def test_nothing_is_formatted_unless_debug_is_on(self):
        log, out = logger(logging.INFO)
        with mock.patch.object(checks, '_where', side_effect=AssertionError('formatted')):
            self.assertFalse(check(log, 'EPISODE_SIZE', False, actual=object()))
        self.assertEqual(out.getvalue(), '')

    def test_require_raises_with_the_check_and_path(self):
        log, out = logger(logging.DEBUG)
        require(log, 'PATH_INSIDE_LIBRARY', True, Stop)
        with self.assertRaises(Stop) as caught:
            require(log, 'PATH_INSIDE_LIBRARY', False, Stop, path='/outside/a.mkv')
        self.assertRegex(caught.exception.failed_check,
                         r'^src/nextgen/tests/test_checks\.py:\d+ test_require_\w* PATH_INSIDE_LIBRARY$')
        self.assertEqual(caught.exception.failed_path, '/outside/a.mkv')
        self.assertIn('check PATH_INSIDE_LIBRARY failed', out.getvalue())

    def test_here_names_the_calling_line(self):
        import inspect
        line = inspect.currentframe().f_lineno + 1
        self.assertEqual(here('SERIES_MISSING'), f'src/nextgen/tests/test_checks.py:{line} test_here_names_the_calling_line SERIES_MISSING')

    def test_check_names_fit_history(self):
        from src.nextgen.reasons import _CHECK
        self.assertTrue(_CHECK.fullmatch(here('A_VALID_NAME_2')))
        for bad in ('lower', 'HAS SPACE', 'X' * 33, ''):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                here(bad)


class WorkerLogsUnfinishedStepsTests(unittest.IsolatedAsyncioTestCase):
    """Every attempt that does not complete logs, at DEBUG, the check that stopped it and why."""
    from src.nextgen.tests.test_api_v1 import ApiV1Tests as _Api
    asyncSetUp = _Api.asyncSetUp

    async def run_one(self, level, outcome):
        from src.nextgen.handlers import HandlerDefinition
        from src.nextgen.job_contracts import Outcome, Registration, Registry
        from src.nextgen.storage.sqlite import connect, enqueue_job, transaction
        from src.nextgen.worker import Policy, Worker
        snap = self.store.read()
        self.store.update({'logging': {'level': level}}, expected_revision=snap.revision)
        db = connect(self.database)
        try:
            with transaction(db):
                enqueue_job(db, kind='TEST', payload={}, component='worker')
        finally:
            db.close()
        async def execute(context):
            return outcome
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        original = logging.StreamHandler.__init__
        with mock.patch.object(logging.StreamHandler, '__init__',
                               lambda self, stream=None: original(self, out)):
            worker = Worker(self.store, self.startup,
                            Registry([Registration('TEST', HandlerDefinition('TEST', 'Test work'), execute)]),
                            policy=Policy(lease_ms=3000))
            try:
                await asyncio.wait_for(worker.attempt(), 10)
            finally:
                await worker.close()
        return out.getvalue()

    async def test_the_failed_check_is_logged_at_debug(self):
        from src.nextgen.job_contracts import Outcome
        text = await self.run_one('DEBUG', Outcome('review', 'ACQUISITION_SIZE_LIMIT', facts={
            'failed_check': 'src/nextgen/acquisition/settings.py:93 admit EPISODE_SIZE', 'video_mb': 2, 'limit_mb': 1}))
        line = next(l for l in text.splitlines() if 'Not finished' in l)
        for part in ('DEBUG', 'job #1 · Test work · Not finished · The video is larger than the episode size limit',
                     'check EPISODE_SIZE failed · acquisition/settings.py:93', 'video 2 MB, limit 1 MB'):
            self.assertIn(part, line)

    async def test_nothing_extra_at_info(self):
        from src.nextgen.job_contracts import Outcome
        text = await self.run_one('INFO', Outcome('review', 'ACQUISITION_SIZE_LIMIT'))
        self.assertNotIn('Not finished', text)



class LogContextTests(unittest.TestCase):
    def test_a_job_logger_names_its_activity_only_on_its_own_lines(self):
        from src.nextgen.diagnostics import event
        log, out = logger(logging.INFO)
        log.nextgen_context = {'activity': 'Reserve episode for acquisition', 'job_id': 3}
        event(log, logging.INFO, 'DONE', job_id=3, attempt=1)
        event(log, logging.INFO, 'CREATED', job_id=4)                # the successor it creates
        first, second = out.getvalue().splitlines()
        self.assertTrue(first.startswith('job #3 · Reserve episode for acquisition · '))
        self.assertEqual(second, 'job #4 · Job created')


if __name__ == '__main__':
    unittest.main()
