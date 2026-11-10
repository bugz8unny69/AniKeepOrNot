"""Read-only per-file AniDB / Anime-Lists / TVDB batch corroboration.

An incoming title and torrent file name are hints. Every regular episode
requires its own unique AniDB episode ID, documented Anime-Lists mapping,
and TVDB episode ID. No download, DB write, staging or trial authorization.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath

from src.anime.acquisition.metadata import BatchInventory, ObservedBatchInventory
from src.anime.batch.selection import (
    VIDEO, _SE, _X, _SEASON, _SEASON_DIR, file_episode_number,
    plan_verified_batch, BatchPlanningError,
)
from src.anime.providers.errors import ProviderInvalidResponse
from src.anime.providers.http import Credentials
from src.anime.providers.release_mapping import forward_mappings, absolute_lookup
from src.anime.release_contracts import ReleaseNotReady, ReleaseConflict


@dataclass(frozen=True)
class VerifiedBatchFile:
    index: int
    path: str
    release_episode: int
    anidb_episode_number: int
    anidb_episode_id: int
    tvdb_season: int
    tvdb_episode: int
    tvdb_episode_id: int


@dataclass(frozen=True)
class VerifiedBatch:
    info_hash: str
    info_sha256: str | None
    anidb_id: int
    tvdb_series_id: int
    season: int
    files: tuple[VerifiedBatchFile, ...]

    def episode_map(self):
        return {f.release_episode: f.tvdb_episode for f in self.files}


def _explicit_tvdb_scope(path):
    relative = PurePosixPath(path)
    stem = relative.stem
    return (bool(_SE.search(stem) or _X.search(stem) or _SEASON.search(stem))
            or any(_SEASON_DIR.fullmatch(p) for p in relative.parts[:-1]))


def verify_batch_files(inventory, *, client, credentials, anidb_id, tvdb_series_id,
                       season, cancelled=None):
    """Verify all regular video files against three provider-backed sources.

    Exact SxxExx/season-folder numbers are treated as TVDB season-relative.
    Bare episode numbers are accepted only when an AniDB or TVDB reading is
    unique; disagreement fails closed instead of guessing an offset.
    """
    if type(inventory) not in (BatchInventory,ObservedBatchInventory):
        raise ValueError('Hash-verified multi-video inventory required')
    if type(credentials) is not Credentials:
        raise ValueError('Provider credentials required')
    for value in (anidb_id, tvdb_series_id, season):
        if type(value) is not int or value < 1:
            raise ValueError('Positive verified series identity required')
    if season > 100:
        raise ValueError('Verified season out of range')
    declared = []
    for file in inventory.files:
        if PurePosixPath(file.path).suffix.lower() not in VIDEO:
            continue
        number = file_episode_number(file.path, season)
        if number is not None:
            declared.append((file, number, _explicit_tvdb_scope(file.path)))
    if not declared:
        raise ReleaseNotReady('RELEASE_TYPE_UNSUPPORTED')
    if cancelled is not None and cancelled.is_set():
        raise ReleaseNotReady()
    catalog = client.episodes(anidb_id)
    if (type(catalog) is not dict or not 1 <= len(catalog) <= 10000
            or any(type(n) is not int or type(eid) is not int
                   or not 1 <= n <= 9999 or eid < 1
                   for n,eid in catalog.items())
            or len(set(catalog.values())) != len(catalog)):
        raise ProviderInvalidResponse()
    mappings = forward_mappings(client.mapping_xml(), anidb_id, tvdb_series_id, catalog,
                                absolute=absolute_lookup(client, tvdb_series_id, credentials))
    reverse = {}
    for anidb_number, target in mappings.items():
        if target in reverse:
            # Merged/colliding episodes are never one-to-one evidence.
            raise ReleaseConflict()
        reverse[target] = anidb_number
    seen_ids, tvdb_records = set(), {}
    for page in range(20):
        if cancelled is not None and cancelled.is_set():
            raise ReleaseNotReady()
        records,more = client.episode_page(tvdb_series_id,page,credentials)
        if type(records) not in (tuple,list) or len(records)>1000:
            raise ProviderInvalidResponse()
        for entry in records:
            if (type(entry) not in (tuple,list) or len(entry)!=3
                    or any(type(v) is not int for v in entry)):
                raise ProviderInvalidResponse()
            eid,record_season,number=entry
            if eid <= 0 or record_season < 0 or number <= 0 or eid in seen_ids:
                raise ProviderInvalidResponse()
            seen_ids.add(eid)
            key=(record_season,number)
            if key in tvdb_records:
                raise ReleaseConflict()
            tvdb_records[key]=eid
        if len(seen_ids)>10000:
            raise ProviderInvalidResponse()
        if not more:
            break
    else:
        raise ProviderInvalidResponse()

    facts, used_anidb, used_tvdb = [], set(), set()
    for file,number,explicit in declared:
        target=(season,number)
        direct = reverse.get(target)
        from_anidb = number if number in mappings and mappings[number][0]==season else None
        if explicit:
            candidate = direct
        elif direct is not None and from_anidb is not None and direct != from_anidb:
            raise ReleaseNotReady('RELEASE_EPISODE_MAPPING_AMBIGUOUS')
        else:
            candidate = direct if direct is not None else from_anidb
        if candidate is None:
            raise ReleaseNotReady('RELEASE_EPISODE_MAPPING_MISSING')
        destination = mappings[candidate]
        tvdb_id = tvdb_records.get(destination)
        if tvdb_id is None:
            raise ReleaseNotReady('RELEASE_TVDB_EPISODE_MISSING')
        if candidate in used_anidb or destination in used_tvdb:
            raise ReleaseConflict()
        used_anidb.add(candidate)
        used_tvdb.add(destination)
        facts.append(VerifiedBatchFile(
            file.index,file.path,number,candidate,catalog[candidate],
            destination[0],destination[1],tvdb_id))
    if cancelled is not None and cancelled.is_set():
        raise ReleaseNotReady()
    return VerifiedBatch(inventory.info_hash,inventory.info_sha256,anidb_id,
                         tvdb_series_id,season,tuple(facts))


def plan_provider_verified_batch(inventory, verified, *, start_episode=1):
    """Cross-check file identity before converting provider facts to a plan."""
    if type(inventory) not in (BatchInventory,ObservedBatchInventory) or type(verified) is not VerifiedBatch:
        raise ValueError('Verified episode facts and inventory required')
    if (inventory.info_hash, inventory.info_sha256) != (verified.info_hash, verified.info_sha256):
        raise BatchPlanningError('Provider proof belongs to different torrent metadata')
    by_index={f.index:f for f in inventory.files}
    for fact in verified.files:
        file=by_index.get(fact.index)
        if (file is None or file.path!=fact.path or fact.tvdb_season!=verified.season):
            raise BatchPlanningError('Provider-verified file identity changed')
    return plan_verified_batch(inventory,season=verified.season,
                               episode_map=verified.episode_map(),
                               start_episode=start_episode)
