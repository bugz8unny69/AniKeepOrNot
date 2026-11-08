"""Focused tests for optional systemd watchdog notification behavior."""
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.anime.systemd_notify import notify, watchdog_interval


class SystemdNotifyTests(unittest.TestCase):
    def test_disabled_without_socket(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify("READY=1"))
            self.assertIsNone(watchdog_interval())

    def test_watchdog_interval_and_pid(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000"}, clear=True):
            self.assertEqual(watchdog_interval(), 30)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": "0"}, clear=True):
            self.assertIsNone(watchdog_interval())

    def test_unix_datagram_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "notify.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(path)
                receiver.settimeout(1)
                with patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                    self.assertTrue(notify("READY=1"))
                self.assertEqual(receiver.recv(128), b"READY=1")


if __name__ == "__main__":
    unittest.main()
