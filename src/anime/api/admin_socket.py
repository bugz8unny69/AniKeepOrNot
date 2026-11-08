"""Restricted local administration socket served inside the API process."""
import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import struct

from src.anime.api.password_store import PasswordConflict, PasswordStorageError

MAX_LINE = 2048


async def serve_admin(reader, writer, password_store):
    try:
        sock = writer.get_extra_info("socket")
        if not hasattr(socket, "SO_PEERCRED") or sock is None:
            raise ValueError("Unauthorized peer")
        pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        # File mode and directory ACLs gate connections; peer identity adds a
        # fail-closed second layer. Group membership is checked by the kernel.
        if uid not in (0, os.geteuid()):
            raise ValueError("Unauthorized peer")
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if len(line) > MAX_LINE or not line.endswith(bytes([10])):
            raise ValueError("Invalid request")
        request = json.loads(line)
        if type(request) is not dict or set(request) not in ({"command", "password"}, {"command", "password", "if_unset"}) or request["command"] != "set-password":
            raise ValueError("Invalid request")
        if "if_unset" in request and type(request["if_unset"]) is not bool:
            raise ValueError("Invalid request")
        revision, _ = password_store.read()
        if request.get("if_unset") and revision:
            response = {"ok": True, "unchanged": True}
        else:
            next_revision = await asyncio.to_thread(password_store.replace, request["password"], expected_revision=revision)
            response = {"ok": True, "revision": next_revision}
    except (ValueError, TypeError, json.JSONDecodeError, PasswordConflict, PasswordStorageError, asyncio.TimeoutError):
        response = {"ok": False}
    except Exception:
        response = {"ok": False}
    try:
        writer.write(json.dumps(response).encode() + bytes([10]))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def start_admin(path, password_store):
    path = Path(path)
    parent = path.parent
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise RuntimeError("Admin socket directory must not be writable by group/others")
    if path.exists() or path.is_symlink():
        raise RuntimeError("Admin socket path already exists")
    server = await asyncio.start_unix_server(lambda r, w: serve_admin(r, w, password_store), path=str(path))
    os.chmod(path, 0o600)
    return server
