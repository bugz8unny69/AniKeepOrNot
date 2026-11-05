"""Statement defects are reported as such, not as unavailable storage."""
import logging
from pathlib import Path
import tempfile
import unittest

from src.nextgen.diagnostics import exception
from src.nextgen.job_contracts import failure_facts
from src.nextgen.reasons import get_reason
from src.nextgen.storage.job_queue import JobQueue, QueueUnavailable, StorageStatementInvalid
from src.nextgen.storage.sqlite import connect, initialize


class StorageStatementErrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / 'db.sqlite'
        db = connect(self.database)
        try:
            initialize(db)
        finally:
            db.close()
        self.queue = JobQueue(self.database)

    def test_missing_binding_is_a_statement_error_with_its_site(self):
        with self.assertRaises(StorageStatementInvalid) as caught:
            with self.queue.connection() as db:
                db.execute('SELECT ?,?', (1,))  # One value for two placeholders.
        error = caught.exception
        # Same recovery handling as before: still a QueueUnavailable.
        self.assertIsInstance(error, QueueUnavailable)
        self.assertEqual(error.reason_code, 'STORAGE_STATEMENT_INVALID')
        self.assertEqual(str(error), 'A database statement is invalid (programming error).')
        # The failing execute() line, not the enclosing `with` line.
        line = next(n for n, text in enumerate(Path(__file__).read_text().splitlines(), 1)
                    if 'One value for two placeholders' in text)
        self.assertEqual(error.failed_check,
                         f'src/nextgen/tests/test_storage_statement_errors.py:{line} '
                         'test_missing_binding_is_a_statement_error_with_its_site PROGRAMMINGERROR')
        self.assertEqual(failure_facts(error), {'failed_check': error.failed_check})
        reason = get_reason('STORAGE_STATEMENT_INVALID')
        self.assertIsNotNone(reason)
        self.assertIs(reason.retryable, False)

    def test_unsupported_parameter_type_is_a_statement_error(self):
        with self.assertRaises(StorageStatementInvalid):
            with self.queue.connection() as db:
                db.execute('SELECT ?', (object(),))

    def test_operational_failures_still_report_storage_unavailable(self):
        with self.assertRaises(QueueUnavailable) as caught:
            with self.queue.connection() as db:
                db.execute('SELECT * FROM table_that_does_not_exist')
        self.assertNotIsInstance(caught.exception, StorageStatementInvalid)
        self.assertEqual(caught.exception.reason_code, 'STORAGE_UNAVAILABLE')

    def test_log_names_the_statement_error_and_original_cause(self):
        try:
            with self.queue.connection() as db:
                db.execute('SELECT ?,?', (1,))
        except StorageStatementInvalid as error:
            with self.assertLogs('test.statement', logging.ERROR) as logs:
                exception(logging.getLogger('test.statement'), 'OPERATION_FAILED', error,
                          reason_code=error.reason_code, component='worker')
        line = logs.output[0]
        self.assertIn('reason_code=STORAGE_STATEMENT_INVALID', line)
        self.assertNotIn('STORAGE_UNAVAILABLE', line)
        self.assertIn('ProgrammingError', line)
        self.assertIn('test_storage_statement_errors.py', line)


if __name__ == '__main__':
    unittest.main()
