"""Shared test setup: a temporary settings folder and a fresh database."""
import tempfile
import unittest
from pathlib import Path

from src.anime.configuration import ConfigurationStore
from src.anime.storage.sqlite import connect, initialize
from src.anime.tests.test_acquisition_metadata import metadata


class DatabaseSetup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'queue.sqlite'
        self.store = ConfigurationStore(self.root)
        self.startup = self.store.initialize({'storage': {'database_path': str(self.database)}}).snapshot
        self.raw, self.digest = metadata()
        self.reads = []
        db = connect(self.database)
        initialize(db)
        db.close()

    def rows(self, sql, args=()):
        db = connect(self.database)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()
