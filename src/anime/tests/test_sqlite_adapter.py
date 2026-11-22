"""The SQLite adapter: a busy database is waited for, never reported as unavailable.

Prod 2026-11-22 06:07: staging, 26 cleanups, transcodes and identification all
wrote at once; one writer waited past the 5 s busy timeout, the job saw
"database is locked" (QueueUnavailable) and the whole worker exited.
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.anime.storage import adapter
from src.anime.storage.sqlite import connect, transaction


class SqliteAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / 'db.sqlite')
        db = connect(self.path)
        db.execute('CREATE TABLE counter(n INTEGER NOT NULL)')
        db.execute('INSERT INTO counter VALUES(0)')
        db.commit()
        db.close()

    def open(self, busy_ms=0):
        db = connect(self.path)
        db.execute(f'PRAGMA busy_timeout={busy_ms}')   # SQLite alone would fail at once.
        return db

    def test_writers_in_one_process_queue_instead_of_failing(self):
        errors = []

        def work():
            db = self.open()
            try:
                for _ in range(20):
                    with transaction(db):
                        n = db.execute('SELECT n FROM counter').fetchone()[0]
                        time.sleep(0.001)
                        db.execute('UPDATE counter SET n=?', (n + 1,))
            except Exception as exc:   # pragma: no cover - the failure being tested
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        db = self.open()
        self.assertEqual(db.execute('SELECT n FROM counter').fetchone()[0], 160)
        db.close()

    def test_a_write_held_by_another_process_is_waited_out(self):
        other = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)   # Like the API.
        other.execute('BEGIN IMMEDIATE')
        threading.Timer(0.4, other.rollback).start()
        db = connect(self.path)   # The adapter's busy timeout, no retries.
        started = time.monotonic()
        with transaction(db):
            db.execute('UPDATE counter SET n=n+1')
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        db.close(); other.close()

    def test_a_slow_write_is_logged_with_the_code_that_opened_it(self):
        db = self.open()
        with self.assertLogs('anime.storage', 'WARNING') as logs:
            with transaction(db):
                time.sleep(adapter.SLOW_TRANSACTION_S + 0.1)
        self.assertIn('test_sqlite_adapter.py', logs.output[0])
        db.close()

    def test_a_second_write_in_the_same_thread_fails_at_once_instead_of_waiting_on_itself(self):
        first, second = self.open(), self.open()
        started = time.monotonic()
        with transaction(first):
            with self.assertRaises(sqlite3.OperationalError):
                with transaction(second):
                    pass
        self.assertLess(time.monotonic() - started, 5)
        with transaction(second):   # Lock released: writing works again.
            second.execute('UPDATE counter SET n=n+1')
        first.close(); second.close()


if __name__ == '__main__':
    unittest.main()
