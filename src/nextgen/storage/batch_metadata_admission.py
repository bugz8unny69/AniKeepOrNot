"""One-shot stopped magnet admission for batch metadata on legacy qB Web APIs.

Admission is recorded under the live VERIFY_RELEASE lease *before* a send.
The remote torrent is never automatically adopted, re-added, or started by
other jobs. Recovered UNCERTAIN rows do not issue another send permit.
"""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import json
import secrets
import threading

from src.nextgen.acquisition.metadata import (
    AcquisitionConflict, MetadataInvalid, ObservedBatchInventory, TorrentFile, infohash,
)
from src.nextgen.acquisition.metadata_service import (
    is_metadata_expired, verify_torrent_ownership, METADATA_TIMEOUT_MS,
)
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.storage.job_queue import JobQueue, Claim
from src.nextgen.storage.migrations import _open, LATEST_VERSION
from src.nextgen.storage.sqlite import transaction
from src.nextgen.acquisition.submission import ClientInvalid

STOPPED = frozenset(('pausedDL','pausedUP','stoppedDL','stoppedUP'))


@dataclass(frozen=True)
class EarlyBatchIntent:
    event_id: int
    job_id: int
    info_hash: str
    marker: str = field(repr=False)
    options: DownloadOptions = field(repr=False)
    api_major: int
    state: str
    inventory_json: str | None = field(default=None,repr=False)

    def __post_init__(self):
        if (type(self.event_id) is not int or self.event_id <= 0
                or type(self.job_id) is not int or self.job_id <= 0
                or infohash(self.info_hash)!=self.info_hash
                or type(self.options) is not DownloadOptions
                or type(self.marker) is not str or len(self.marker)!=32
                or any(x not in '0123456789abcdef' for x in self.marker)
                or self.api_major not in (4,5)
                or self.state not in ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED','ADOPTED','RELEASED')):
            raise AcquisitionConflict()


class EarlyBatchPermit:
    def __init__(self, event_id, marker):
        self.event_id,self.marker=event_id,marker
        self.lock=threading.Lock()
        self.used=False

    def take(self,intent):
        with self.lock:
            if (self.used or type(intent) is not EarlyBatchIntent
                    or intent.event_id!=self.event_id or intent.marker!=self.marker
                    or intent.state!='UNCERTAIN'):
                raise ClientInvalid()
            self.used=True


def _restore(row):
    if row is None:
        return None
    event_id,job_id,digest,marker,endpoint,root,category,timeout,major,state,inventory=row
    return EarlyBatchIntent(event_id,job_id,digest,marker,
                            DownloadOptions(endpoint,root,category,timeout),
                            major,state,inventory)


def _select(db,*,event_id=None,info_hash=None):
    if (event_id is None)==(info_hash is None):
        raise ValueError('One early admission locator required')
    where,args=('event_id=?',(event_id,)) if event_id is not None else (
        "info_hash=? AND state NOT IN ('RELEASED','EXTERNAL')",(info_hash,))
    return _restore(db.execute(
        'SELECT event_id,verification_job_id,info_hash,ownership_marker,'
        'base_url,save_path,category,timeout_seconds,api_major,state,inventory_json '
        'FROM batch_metadata_admissions WHERE '+where,args).fetchone())


def _inventory(intent):
    if intent.inventory_json is None:
        return None
    try:
        raw=json.loads(intent.inventory_json)
        if type(raw) is not dict or set(raw)!={'files'} or type(raw['files']) is not list:
            raise MetadataInvalid()
        result=ObservedBatchInventory(intent.info_hash,tuple(TorrentFile(**f) for f in raw['files']))
        if result.json()!=intent.inventory_json:
            raise MetadataInvalid()
        return result
    except (TypeError,KeyError,ValueError):
        raise MetadataInvalid() from None


def lookup(database,digest):
    """Read-only lookup for downstream batch jobs; no permits or mutations."""
    digest=infohash(digest)
    db=_open(database,readonly=True,timeout_ms=5000)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:
            raise AcquisitionConflict()
        intent=_select(db,info_hash=digest)
    finally:
        db.close()
    if intent is None or intent.state not in ('OWNED_STOPPED','ADOPTED'):
        return None,None
    return intent,_inventory(intent)


class BatchMetadataJournal:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def _owns(self,db,claim,event_id):
        if (type(claim) is not Claim or claim.kind!='VERIFY_RELEASE'
                or claim.event_id!=event_id):
            raise AcquisitionConflict()
        self.queue._owns(db,claim,self.queue.now())
        row=db.execute("SELECT 1 FROM jobs WHERE job_id=? AND event_id=? AND kind='VERIFY_RELEASE'",
                       (claim.job_id,event_id)).fetchone()
        if row is None:
            raise AcquisitionConflict()

    def authorize(self,claim,event_id,digest,options,major,present):
        """Persist the sole admission before returning an ephemeral send permit."""
        digest=infohash(digest)
        if (type(options) is not DownloadOptions or major not in (4,5)
                or type(present) is not bool):
            raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,event_id)
            prior=_select(db,event_id=event_id)
            if prior is not None:
                if (prior.job_id!=claim.job_id or prior.info_hash!=digest
                        or prior.api_major!=major
                        or prior.options.base_url!=options.base_url
                        or prior.options.category!=options.category
                        or prior.options.timeout_seconds!=options.timeout_seconds
                        or prior.options.download_root!=str(
                            PurePosixPath(options.download_root)/('ad-'+prior.marker))):
                    raise AcquisitionConflict()
                return prior,None
            # No other recorded admission may own the same infohash.
            if (db.execute('SELECT 1 FROM batch_torrent_submissions WHERE info_hash=?',
                           (digest,)).fetchone()
                    or _select(db,info_hash=digest) is not None):
                raise AcquisitionConflict()
            marker=secrets.token_hex(16)
            root=str(PurePosixPath(options.download_root)/('ad-'+marker))
            state='EXTERNAL' if present else 'UNCERTAIN'
            now=self.queue.now()
            db.execute('INSERT INTO batch_metadata_admissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (event_id,claim.job_id,digest,marker,options.base_url,root,
                        options.category,options.timeout_seconds,major,state,None,now,now))
            return _select(db,event_id=event_id),(None if present else EarlyBatchPermit(event_id,marker))

    def guard(self,claim,intent):
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,intent.event_id)
            current=_select(db,event_id=intent.event_id)
            if current!=intent or current.state!='UNCERTAIN':
                raise AcquisitionConflict()

    def metadata_expired(self, claim, event_id, *, timeout_ms=METADATA_TIMEOUT_MS):
        """Ten-minute acquisition limit; no repeat magnet send is permitted."""
        if type(timeout_ms) is not int or timeout_ms < 60000:
            raise AcquisitionConflict()
        with self.queue.connection() as db, transaction(db):
            now = self.queue.now()
            self._owns(db, claim, event_id)
            current = _select(db, event_id=event_id)
            if current is None:
                raise AcquisitionConflict()
            row = db.execute('SELECT created_at_ms FROM batch_metadata_admissions WHERE event_id=?',
                             (event_id,)).fetchone()
            if row is None:
                raise AcquisitionConflict()
            return is_metadata_expired(row[0], now, timeout_ms=timeout_ms)

    def observe(self,claim,intent,observed):
        """Only stable and strictly owned, stopped inventory may become durable."""
        with self.queue.connection() as db,transaction(db):
            self._owns(db,claim,intent.event_id)
            current=_select(db,event_id=intent.event_id)
            if current is None or current.info_hash!=intent.info_hash:
                raise AcquisitionConflict()
            if current.state in ('EXTERNAL','REVIEW','ADOPTED'):
                return current
            if current.state=='OWNED_STOPPED':
                return current
            if observed is None:
                return current
            if (observed.info_hash!=intent.info_hash
                    or not verify_torrent_ownership(observed, intent.marker,
                                                    intent.options.download_root,
                                                    intent.options.category)):
                state,raw='REVIEW',None
            elif observed.state not in STOPPED or not observed.files:
                return current
            else:
                inv=ObservedBatchInventory(intent.info_hash,observed.files)
                state,raw='OWNED_STOPPED',inv.json()
            now=self.queue.now()
            db.execute('UPDATE batch_metadata_admissions SET state=?,inventory_json=?,updated_at_ms=? '
                       'WHERE event_id=? AND state=?',(state,raw,now,intent.event_id,current.state))
            return _select(db,event_id=intent.event_id)
