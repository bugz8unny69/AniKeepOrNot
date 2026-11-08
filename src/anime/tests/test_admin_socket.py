"""Same-process admin socket integration tests (no HTTP listener)."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import socket
import struct

from src.anime.api.admin_socket import start_admin
from src.anime.api.password_store import PasswordStore


class AdminSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_password_setup_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = str(Path(directory) / "admin.sock")
            store = PasswordStore(directory)
            server = await start_admin(path, store)
            try:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(json.dumps({"command": "set-password", "password": "correct horse battery staple"}).encode() + bytes([10]))
                await writer.drain()
                result = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
                self.assertTrue(result["ok"])
                self.assertTrue(store.verify("correct horse battery staple")[1])
                writer.close()
                await writer.wait_closed()
                invalid_reader, invalid_writer = await asyncio.open_unix_connection(path)
                invalid_writer.write(json.dumps({"command": "set-password", "password": "x", "extra": "unexpected"}).encode() + bytes([10]))
                await invalid_writer.drain()
                invalid_result = json.loads(await asyncio.wait_for(invalid_reader.readline(), timeout=5))
                self.assertFalse(invalid_result["ok"])
                invalid_writer.close()
                await invalid_writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()
                Path(path).unlink(missing_ok=True)

    async def test_if_unset_preserves_password_and_rejects_unprivileged_peer(self):
        from src.anime.api.admin_socket import serve_admin
        class Peer:
            uid=os.geteuid()
            def getsockopt(self,*args):return struct.pack('3i',123,self.uid,123)
        class Writer:
            def __init__(self):self.response=b''
            def get_extra_info(self,name):return Peer()
            def write(self,data):self.response+=data
            async def drain(self):pass
            def close(self):pass
            async def wait_closed(self):pass
        async def send(store,**request):
            reader=asyncio.StreamReader()
            reader.feed_data(json.dumps(request).encode()+b'\n')
            reader.feed_eof()
            writer=Writer()
            await serve_admin(reader,writer,store)
            return json.loads(writer.response)
        with tempfile.TemporaryDirectory() as directory:
            store=PasswordStore(directory)
            result=await send(store,command='set-password',password='first password for testing',if_unset=True)
            self.assertTrue(result['ok'])
            result=await send(store,command='set-password',password='replacement password for testing',if_unset=True)
            self.assertTrue(result['unchanged'])
            self.assertTrue(store.verify('first password for testing')[1])
            Peer.uid=os.geteuid()+1000
            result=await send(store,command='set-password',password='unauthorized password')
            self.assertFalse(result['ok'])
            self.assertTrue(store.verify('first password for testing')[1])

    async def test_api_shutdown_accepts_socket_removed_by_asyncio(self):
        from src.anime.api.app import Application
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'admin.sock'
            path.touch()
            class Server:
                def close(self):pass
                async def wait_closed(self):path.unlink()
            app=object.__new__(Application)
            app.admin_path=str(path)
            app.passwords=object()
            app.admin_server=None
            app.close=lambda:None
            messages=iter([{'type':'lifespan.startup'},{'type':'lifespan.shutdown'}])
            output=[]
            async def receive():return next(messages)
            async def send(message):output.append(message)
            with patch('src.anime.api.app.start_admin',new=AsyncMock(return_value=Server())), \
                 patch('src.anime.api.app.watchdog_interval',return_value=None):
                await app({'type':'lifespan'},receive,send)
            self.assertEqual([message['type'] for message in output],
                ['lifespan.startup.complete','lifespan.shutdown.complete'])

    async def test_reject_unsafe_socket_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o777)
            with self.assertRaises(RuntimeError):
                await start_admin(str(Path(directory) / "admin.sock"), PasswordStore(directory))


if __name__ == "__main__":
    unittest.main()
