"""Native systemd notifications and event-loop watchdog behavior."""
import asyncio
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.anime.systemd_notify import notify, watchdog_interval
from src.anime.api.app import Application


class NotifyTests(unittest.TestCase):
    def test_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify("READY=1"))
            self.assertIsNone(watchdog_interval())

    def test_interval_and_pid(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000"}, clear=True):
            self.assertEqual(watchdog_interval(), 30)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": "0"}, clear=True):
            self.assertIsNone(watchdog_interval())

    def test_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "notify.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(path)
                receiver.settimeout(1)
                with patch.dict(os.environ, {"NOTIFY_SOCKET": path}, clear=True):
                    self.assertTrue(notify("READY=1"))
                self.assertEqual(receiver.recv(128), b"READY=1")


class ApiWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_runs_on_event_loop(self):
        instance = object.__new__(Application)
        instance.closed = False
        signals = []
        with patch("src.anime.api.app.notify", side_effect=signals.append):
            task = asyncio.create_task(instance._watchdog(0.01))
            await asyncio.sleep(0.035)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIn("WATCHDOG=1", signals)


if __name__ == "__main__":
    unittest.main()
