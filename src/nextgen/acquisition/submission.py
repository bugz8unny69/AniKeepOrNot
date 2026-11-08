"""Pinned submission facts and snapshot classification; no external writes."""
from dataclasses import dataclass, field
import json
import re
import threading

from src.nextgen.acquisition.metadata import Manifest, ObservedInventory, TorrentFile, infohash
from src.nextgen.acquisition.metadata_service import STOPPED
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.history import integer


class ClientUnavailable(RuntimeError):
    reason_code='DOWNLOAD_CLIENT_UNAVAILABLE'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The download client could not be reached safely.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class ClientInvalid(ValueError):
    reason_code='DOWNLOAD_CLIENT_RESPONSE_INVALID'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The download client returned information that needs review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class ClientAuthenticationFailed(ValueError):
    reason_code='DOWNLOAD_CLIENT_AUTH_FAILED'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The download client rejected authentication.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class ClientUnsupported(ValueError):
    reason_code='DOWNLOAD_CLIENT_UNSUPPORTED'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'This download client version is not supported.')
        self.failed_check = failed_check
        self.failed_path = failed_path


@dataclass(frozen=True)
class SubmissionPlan:
    preparation_job_id: int
    plan_id: int
    evaluation_job_id: int
    event_id: int
    marker: str = field(repr=False)
    manifest: Manifest | ObservedInventory = field(repr=False)
    def __post_init__(self):
        for name in ('preparation_job_id','plan_id','evaluation_job_id','event_id'):integer(getattr(self,name),positive=True)
        if type(self.marker) is not str or not re.fullmatch('[a-f0-9]{32}',self.marker) or type(self.manifest) not in (Manifest, ObservedInventory):
            raise ClientInvalid()


@dataclass(frozen=True)
class Intent:
    plan: SubmissionPlan = field(repr=False)
    options: DownloadOptions = field(repr=False)
    state: str
    fingerprint: str
    api_major: int
    def __post_init__(self):
        if (type(self.plan) is not SubmissionPlan or type(self.options) is not DownloadOptions
                or self.state not in ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED')
                or type(self.fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}',self.fingerprint)
                or type(self.api_major) is not int or self.api_major not in (4,5)):raise ClientInvalid()


@dataclass(frozen=True)
class Observation:
    info_hash: str
    state: str
    save_path: str = field(repr=False)
    category: str
    tags: tuple[str,...] = field(repr=False)
    auto_tmm: bool
    files: tuple[TorrentFile,...] = field(repr=False)
    def __post_init__(self):
        if infohash(self.info_hash)!=self.info_hash or type(self.auto_tmm) is not bool:raise ClientInvalid()
        for value,limit in ((self.state,64),(self.save_path,4096),(self.category,256)):
            if type(value) is not str or len(value)>limit or any(ord(c)<32 or ord(c)==127 for c in value):raise ClientInvalid()
        if type(self.tags) is not tuple or len(self.tags)>128:raise ClientInvalid()
        if any(type(t) is not str or len(t)>128 or any(ord(c)<32 or ord(c)==127 for c in t) for t in self.tags):raise ClientInvalid()
        if type(self.files) is not tuple or len(self.files)>10000 or any(type(f) is not TorrentFile or f.index!=i for i,f in enumerate(self.files)):
            raise ClientInvalid()


class SendPermit:
    """Process-local, single-use permission returned only by a new journal insert."""
    def __init__(self,fingerprint):
        self.fingerprint=fingerprint
        self._lock=threading.Lock()
        self._used=False
    def take(self,intent):
        with self._lock:
            if self._used or self.fingerprint!=intent.fingerprint:raise ClientInvalid()
            self._used=True


def manifest_from_json(info_hash,info_sha256,encoded):
    if type(encoded) is not str or len(encoded.encode())>1048576:raise ClientInvalid()
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ClientInvalid()
            result[key]=value
        return result
    try:
        value=json.loads(encoded,object_pairs_hook=pairs)
        observed=info_sha256 is None
        keys={'files','selected_index','evidence','info_hash'} if observed else {'files','selected_index'}
        if type(value) is not dict or set(value)!=keys or type(value['files']) is not list:raise ClientInvalid()
        if observed and (value['evidence']!='qbittorrent_stopped_inventory' or value['info_hash']!=info_hash):raise ClientInvalid()
        files=[]
        for row in value['files']:
            if type(row) is not dict or set(row)!={'index','path','size'}:raise ClientInvalid()
            files.append(TorrentFile(row['index'],row['path'],row['size']))
        result=(ObservedInventory(info_hash,tuple(files),value['selected_index']) if observed
                else Manifest(info_hash,info_sha256,tuple(files),value['selected_index']))
        if result.json()!=encoded:raise ClientInvalid()
        return result
    except (ValueError,TypeError,KeyError,RecursionError):raise ClientInvalid() from None


def classify(intent,observation):
    """Only exact provenance/destination/metadata in a stopped state can confirm."""
    if observation is None:return 'UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN'
    if type(observation) is not Observation:raise ClientInvalid()
    plan=intent.plan
    if (observation.info_hash!=plan.manifest.info_hash or observation.save_path.rstrip('/')!=intent.options.download_root
            or observation.category!=intent.options.category or observation.auto_tmm is not False
            or 'ad-'+plan.marker not in observation.tags
            or any(tag.startswith('ad-') and tag!='ad-'+plan.marker for tag in observation.tags)):
        return 'REVIEW','TORRENT_OWNERSHIP_CONFLICT'
    if observation.state in ('checkingDL','checkingUP','checkingResumeData','allocating') or not observation.files:
        return 'UNCERTAIN','TORRENT_SUBMISSION_UNCERTAIN'
    if observation.state not in STOPPED or observation.files!=plan.manifest.files:
        return 'REVIEW','TORRENT_OWNERSHIP_CONFLICT'
    return 'OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED'
