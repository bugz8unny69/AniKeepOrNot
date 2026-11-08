"""Offline admission/drain/recovery tests. Authored, intentionally unexecuted."""
from multiprocessing import get_context
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from src.anime.maintenance import (
    MaintenanceGate, MaintenanceBusy, MaintenanceError, MaintenancePending,
)


def admitted_worker(path, entered, release):
    with MaintenanceGate(path).operation():
        entered.set()
        release.wait(10)


def interrupted_controller(path, ticket):
    with MaintenanceGate(path).exclusive(ticket):
        os._exit(0)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "anime.sqlite"
        self.gate = MaintenanceGate(self.path)

    def test_request_blocks_new_work_and_drains_existing_without_canceling(self):
        with self.gate.operation():
            ticket = self.gate.request()
            self.assertEqual(ticket, self.gate.request())
            with self.assertRaises(MaintenancePending):
                with MaintenanceGate(self.path).operation():
                    self.fail("new admission")
            with self.assertRaises(MaintenanceBusy):
                with self.gate.exclusive(ticket):
                    self.fail("exclusive before drain")
        with self.gate.exclusive(ticket) as lease:
            lease.require(self.path)
            with self.assertRaises(MaintenanceError):
                lease.require(self.path.with_name("other.sqlite"))
            lease.complete()
        self.assertIsNone(self.gate.status())
        with self.assertRaises(MaintenanceError):
            lease.require(self.path)
        with self.gate.operation():
            pass

    def test_exception_or_missing_complete_keeps_durable_marker(self):
        ticket = self.gate.request()
        with self.gate.exclusive(ticket):
            pass
        self.assertEqual(MaintenanceGate(self.path).status(), ticket)
        with self.assertRaises(ValueError):
            with self.gate.exclusive(ticket) as lease:
                lease.complete()
                raise ValueError("failure")
        self.assertEqual(self.gate.status(), ticket)
        with self.assertRaises(MaintenanceError):
            with self.gate.exclusive("0" * 32):
                self.fail("wrong ticket")

    def test_separate_process_admission_blocks_exclusive_until_release(self):
        context = get_context("spawn")
        entered, release = context.Event(), context.Event()
        worker = context.Process(target=admitted_worker, args=(self.path, entered, release))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            ticket = self.gate.request()
            with self.assertRaises(MaintenanceBusy):
                with self.gate.exclusive(ticket):
                    self.fail("worker still active")
        finally:
            release.set()
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        with self.gate.exclusive(ticket) as lease:
            lease.complete()

    def test_controller_process_death_does_not_reopen_admission(self):
        ticket = self.gate.request()
        worker = get_context("spawn").Process(target=interrupted_controller, args=(self.path, ticket))
        worker.start()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(self.gate.status(), ticket)
        with self.assertRaises(MaintenancePending):
            with self.gate.operation():
                self.fail("crash reopened work")
        with self.gate.exclusive(ticket) as lease:
            lease.complete()

    def test_corrupt_marker_symlink_and_fifo_fail_closed(self):
        marker = self.path.parent / ("." + self.path.name + ".nextgen-pending")
        marker.write_bytes(b"broken")
        with self.assertRaises(MaintenanceError):
            with self.gate.operation():
                self.fail("corrupt state")
        marker.unlink()
        other = self.path.parent / "other"
        other.write_text("0" * 32)
        marker.symlink_to(other)
        with self.assertRaises(MaintenanceError):
            self.gate.status()
        marker.unlink()
        os.mkfifo(marker)
        with self.assertRaises(MaintenanceError):
            self.gate.status()

    def test_failed_resume_directory_sync_recloses_admission(self):
        ticket = self.gate.request()
        real_fsync = os.fsync
        calls = 0
        def fail_first(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected")
            return real_fsync(fd)
        with patch("src.anime.maintenance.os.fsync", side_effect=fail_first):
            with self.assertRaises(MaintenanceError):
                with self.gate.exclusive(ticket) as lease:
                    lease.complete()
        self.assertEqual(self.gate.status(), ticket)

    def test_handler_io_errors_keep_their_original_classification(self):
        failure = OSError("handler failure")
        with self.assertRaises(OSError) as raised:
            with self.gate.operation():
                raise failure
        self.assertIs(raised.exception, failure)
        ticket = self.gate.request()
        with self.assertRaises(OSError) as raised:
            with self.gate.exclusive(ticket):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.gate.status(), ticket)
