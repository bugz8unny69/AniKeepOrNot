"""Staging journal for the series job: the Publisher's records, kept in the torrent's entry.

staging.Publisher records each temporary file before it creates it, and each
publication before and after the rename, so an interrupted copy is resumed or
cleaned up, never guessed. For the old jobs those records were rows keyed by
job; here they are torrent["staging"]["artifacts"] in the series row.

The Publisher runs in a thread (it copies whole videos), so this journal writes
through its own connection, only the one JSON path of this torrent, and only
while the series lease is still held. It updates the in-memory torrent too, so
the attempt's final save keeps the same records.
"""
from contextlib import closing
import json
import uuid

from src.anime.series_job.store import LeaseLost
from src.anime.storage.sqlite import connect, transaction


def write_torrent_key(database, claim, torrents, torrent, key):
    """Save torrent[key] to the row now, from any thread, only while the lease is held."""
    index = next(i for i, t in enumerate(torrents) if t is torrent)
    with closing(connect(database)) as db, transaction(db):
        changed = db.execute(
            "UPDATE series SET torrents_json=json_set(torrents_json,?,json(?)) WHERE anidb_id=? AND lease_token=?",
            (f"$[{index}].{key}", json.dumps(torrent[key]), claim.anidb_id, claim.token)).rowcount
    if changed != 1:
        raise LeaseLost(f"Lease on series {claim.anidb_id} lost")


class SeriesStagingJournal:
    def __init__(self, database, claim, torrents, torrent):
        self.database, self.claim, self.torrents, self.torrent = database, claim, torrents, torrent

    def _write(self):
        write_torrent_key(self.database, self.claim, self.torrents, self.torrent, "staging")

    def _artifacts(self):
        return self.torrent.setdefault("staging", {}).setdefault("artifacts", [])

    def guard(self, claim):
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT lease_token FROM series WHERE anidb_id=?", (self.claim.anidb_id,)).fetchone()
        if row is None or row[0] != self.claim.token:
            raise LeaseLost(f"Lease on series {self.claim.anidb_id} lost while staging")

    def artifact(self, claim, root, temporary):
        return next((a for a in self._artifacts() if (a["root"], a["temporary"]) == (root, temporary)), None)

    def register(self, claim, root, temporary):
        artifact = {"id": uuid.uuid4().hex, "root": root, "temporary": temporary, "phase": "TEMP", "file": None}
        self._artifacts().append(artifact)
        self._write()
        return artifact

    def register_move(self, claim, root, temporary, source_info):
        # A transcoded file in the staging folder is moved into place; the record names its inode.
        artifact = {"id": uuid.uuid4().hex, "root": root, "temporary": temporary, "phase": "TEMP",
                    "file": list(source_info[:2])}
        self._artifacts().append(artifact)
        self._write()
        return artifact

    def _phase(self, artifact_id, phase):
        for artifact in self._artifacts():
            if artifact["id"] == artifact_id:
                artifact["phase"] = phase
                self._write()
                return
        raise KeyError(artifact_id)

    def intent(self, claim, artifact_id):
        self._phase(artifact_id, "PUBLISH_UNCERTAIN")

    def confirm(self, claim, artifact_id):
        self._phase(artifact_id, "PUBLISHED")
