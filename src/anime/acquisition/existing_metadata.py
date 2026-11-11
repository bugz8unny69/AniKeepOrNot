"""Read-only observation of a torrent that already exists in qBittorrent.

Used as *evidence* only: by the batch candidate controller to rank releases
whose file list is already known, and by batch release evaluation to verify a
release without rediscovering its metadata. It never stops, starts, tags,
re-categorizes or adopts a torrent, and it grants no permission to download
or control one. Downloading or controlling a torrent still requires the
normal journal ownership and authorization checks (submission journals and
BatchMetadataJournal), which treat any torrent they did not admit as EXTERNAL.
"""
from dataclasses import dataclass, field
import json

from src.anime.acquisition.metadata import MetadataInvalid, ObservedBatchInventory, infohash

# qBittorrent reports these while it is still resolving a magnet; the file
# list is not final until the state changes.
METADATA_PENDING = frozenset(('metaDL', 'forcedMetaDL'))
# Bound the read-only inspections one coordination may issue.
MAX_OBSERVATIONS = 25


@dataclass(frozen=True)
class ExistingMetadata:
    """A stable, complete file list read from an existing torrent."""
    inventory: ObservedBatchInventory = field(repr=False)
    state: str
    category: str
    # Informational provenance: the AniKeepOrNot ownership tag the torrent
    # carries, if any. Never used to authorize anything.
    ownership_tag: str | None

    @property
    def info_hash(self):
        return self.inventory.info_hash

    def evidence_json(self):
        return json.dumps({
            'source': 'qbittorrent_existing',
            'state': self.state,
            'ownership_tag': self.ownership_tag,
            'files': [{'index': f.index, 'path': f.path, 'size': f.size} for f in self.inventory.files],
        }, sort_keys=True, separators=(',', ':'))


def observe_existing_metadata(client, digest):
    """Read an existing torrent's complete file list twice, without mutation.

    Returns ExistingMetadata, or None when the torrent is absent, still
    resolving metadata, changed between the two reads, or does not describe a
    usable batch inventory. qBittorrent client errors propagate unchanged.
    """
    digest = infohash(digest)
    first = client.inspect(digest, include_files=True)
    if first is None or first.info_hash != digest or not first.files or first.state in METADATA_PENDING:
        return None
    second = client.inspect(digest, include_files=True)
    if second is None or second.info_hash != digest or second.files != first.files:
        return None
    try:
        inventory = ObservedBatchInventory(digest, second.files)
    except MetadataInvalid:
        return None
    tags = [t for t in second.tags if t.startswith('ad-')]
    return ExistingMetadata(inventory, second.state, second.category, tags[0] if len(tags) == 1 else None)


def make_existing_observer(secrets, *, client_factory=None):
    """observe(configuration, digests) -> {digest: ExistingMetadata} over one qB login."""
    def observe(configuration, digests):
        wanted = tuple(dict.fromkeys(infohash(d) for d in digests))[:MAX_OBSERVATIONS]
        if not wanted:
            return {}
        from src.anime.torrent_client.qbittorrent import Client
        from src.anime.acquisition.settings import download_options
        client = (client_factory or Client)(download_options(configuration), secrets.qbittorrent_credentials())
        client.login()
        found = {}
        for digest in wanted:
            existing = observe_existing_metadata(client, digest)
            if existing is not None:
                found[digest] = existing
        return found
    return observe
