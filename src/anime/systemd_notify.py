"""Optional systemd notification transport; no external dependency.

Only NOTIFY_SOCKET supplied by the service manager enables notifications.
The watchdog interval is derived from WATCHDOG_USEC, not hard-coded.
"""
import os
import socket


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), address)
        return True
    except OSError:
        return False


def watchdog_interval():
    try:
        pid = os.environ.get("WATCHDOG_PID")
        if pid and int(pid) != os.getpid():
            return None
        usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        return usec / 2_000_000 if usec > 0 else None
    except ValueError:
        return None
