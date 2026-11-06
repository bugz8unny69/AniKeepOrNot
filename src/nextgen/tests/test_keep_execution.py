"""Offline shared Keep orchestration tests: no Sonarr or file I/O."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.nextgen.jobs.keep_execution import execute_keep
from src.nextgen.jobs.keep_media_job import registration as single_registration
from src.nextgen.jobs.keep_batch_job import registration as batch_registration
from src.nextgen.sonarr import KeepReview, KeepUnavailable, KeepConfiguration
from src.nextgen.staging import StagingBusy


class KeepExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.results = []
        self.context = SimpleNamespace(
            claim=SimpleNamespace(job_id=101),
            cancelled=threading.Event(),
            configuration=SimpleNamespace(value=lambda key: {
                'storage.database_path': '/tmp/keep-execution-test.sqlite',
                'jellyfin.base_url': 'http://jellyfin.example',
                'shoko.base_url': 'http://shoko.example',
            }[key]))
        self.actions = SimpleNamespace(bind=lambda claim, action: 99)
        patcher = patch('src.nextgen.jobs.keep_execution.ManagementActions',
                        return_value=self.actions)
        self.addCleanup(patcher.stop)
        patcher.start()

    def service(self, configuration, secrets, **kwargs):
        calls = self.calls
        results = self.results
        class FakeService:
            def run(self, claim, target, cancelled):
                calls.append((target, kwargs.get('journal_factory')))
                result = results.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result
        return FakeService()

    async def test_single_handler_uses_same_executor_and_preserves_shoko(self):
        self.results = [True]
        result = await single_registration(object(), service_factory=self.service).execute(
            self.context)
        self.assertEqual(result.kind, 'complete')
        self.assertEqual(result.value, {'refresh': True, 'shoko': True})
        self.assertEqual(self.calls, [(99, None)])

    async def test_multiple_batch_journals_stop_at_first_incomplete_import(self):
        first = lambda database: None
        second = lambda database: None
        journals = lambda actions, target: (first, second)
        self.results = [True, False]
        result = await execute_keep(self.context, object(), service_factory=self.service,
                                    journals=journals)
        self.assertEqual(result.kind, 'defer')
        self.assertEqual(result.reason_code, 'KEEP_IMPORT_WAITING')
        self.assertEqual(result.delay_ms, 30000)
        self.assertEqual(self.calls, [(99, first), (99, second)])
        self.calls.clear()
        self.results = [True, True]
        result = await execute_keep(self.context, object(), service_factory=self.service,
                                    journals=journals)
        self.assertEqual(result.kind, 'complete')
        self.assertEqual(result.value, {'refresh': True})
        self.assertEqual(self.calls, [(99, first), (99, second)])

    async def test_batch_handler_uses_shared_execution_for_each_published_episode(self):
        one = lambda database: None
        two = lambda database: None
        self.results = [True, True]
        with patch('src.nextgen.jobs.keep_batch_job._episode_journals',
                   return_value=(one, two)):
            result = await batch_registration(object(), service_factory=self.service).execute(
                self.context)
        self.assertEqual(result.kind, 'complete')
        self.assertEqual(self.calls, [(99, one), (99, two)])

    async def test_empty_batch_inventory_cannot_report_success(self):
        result = await execute_keep(
            self.context, object(), service_factory=self.service,
            journals=lambda actions, target: ())
        self.assertEqual(result.kind, 'review')
        self.assertEqual(result.reason_code, 'KEEP_IMPORT_REVIEW')
        self.assertEqual(self.calls, [])

    async def test_transient_and_permanent_errors_are_consistent(self):
        cases = (
            (KeepUnavailable(), 'retry', 'SERVICE_UNAVAILABLE'),
            (OSError('storage offline'), 'retry', 'SERVICE_UNAVAILABLE'),
            (KeepReview(), 'review', 'KEEP_IMPORT_REVIEW'),
            (KeepConfiguration(), 'review', 'KEEP_CONFIGURATION_INVALID'),
            (PermissionError('unsafe source'), 'review', 'KEEP_IMPORT_REVIEW'),
            (StagingBusy(), 'defer', 'KEEP_IMPORT_WAITING'),
        )
        for problem, kind, reason in cases:
            with self.subTest(problem=type(problem).__name__):
                self.calls.clear()
                self.results = [problem]
                result = await execute_keep(self.context, object(),
                                            service_factory=self.service)
                self.assertEqual((result.kind, result.reason_code), (kind, reason))
