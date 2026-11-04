"""Bounded BEP 3 v1 file inventory from exact hash-bound metainfo bytes."""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata

from src.nextgen.history import integer, MAX_INTEGER

MAX_METAINFO=4*1024*1024


class MetadataInvalid(ValueError):
    reason_code='ACQUISITION_METADATA_INVALID'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The torrent file list could not be verified.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class SourceMissing(ValueError):
    reason_code='ACQUISITION_SOURCE_MISSING'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'A supported torrent identity is required before acquisition.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class AcquisitionConflict(ValueError):
    reason_code='ACQUISITION_PLAN_CONFLICT'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'Acquisition details changed and need review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


def infohash(value):
    if type(value) is not str or not re.fullmatch('[0-9a-fA-F]{40}',value):raise SourceMissing()
    return value.lower()


def hash_magnet(digest,value=None):
    """A magnet bound to exactly one v1 info hash (Legacy webhook behavior).

    A supplied magnet keeps only its xt, dn and plain public announce
    trackers; trackers with credentials, queries or per-user paths are
    dropped. With nothing usable supplied, qBittorrent resolves the bare
    hash through DHT/PEX exactly as Legacy did. Returns None on a hash
    mismatch, so a conflicting magnet is never silently replaced.
    """
    from urllib.parse import parse_qs, urlencode, urlsplit
    digest=infohash(digest)
    bare='magnet:?xt=urn:btih:'+digest
    if value is None or value=='':return bare
    if type(value) is not str or len(value)>8192 or not value.startswith('magnet:?'):return None
    try:fields=parse_qs(urlsplit(value).query)
    except ValueError:return None
    xt=fields.get('xt',[])
    if len(xt)!=1 or xt[0].lower()!='urn:btih:'+digest:return None
    keep=[('xt','urn:btih:'+digest)]
    names=fields.get('dn',[])
    if len(names)==1 and len(names[0])<=1024 and all(32<=ord(c)!=127 for c in names[0]):
        keep.append(('dn',names[0]))
    for tracker in fields.get('tr',[])[:20]:
        try:parts=urlsplit(tracker)
        except ValueError:continue
        if (parts.scheme in ('http','https','udp') and parts.hostname and '@' not in parts.netloc
                and not parts.query and not parts.fragment and parts.path in ('','/','/announce')):
            keep.append(('tr',tracker))
    return 'magnet:?'+urlencode(keep,safe=':/')


def safe_path(value):
    if (type(value) is not str or not 1<=len(value)<=2048 or '\\' in value or ':' in value
            or any(unicodedata.category(c).startswith('C') for c in value)
            or unicodedata.normalize('NFC',value)!=value):raise MetadataInvalid()
    parts=value.split('/')
    if any(not p or p in ('.','..') or p!=p.strip() or p.endswith('.') for p in parts):raise MetadataInvalid()
    return value


@dataclass(frozen=True)
class TorrentFile:
    index: int
    path: str = field(repr=False)
    size: int
    def __post_init__(self):
        integer(self.index)
        integer(self.size)
        safe_path(self.path)


@dataclass(frozen=True)
class Manifest:
    info_hash: str
    info_sha256: str
    files: tuple[TorrentFile,...] = field(repr=False)
    selected_index: int
    def __post_init__(self):
        if infohash(self.info_hash)!=self.info_hash:raise MetadataInvalid()
        if type(self.info_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',self.info_sha256):raise MetadataInvalid()
        if type(self.files) is not tuple or not 1<=len(self.files)<=10000:raise MetadataInvalid()
        if any(type(f) is not TorrentFile or f.index!=i for i,f in enumerate(self.files)):raise MetadataInvalid()
        paths=[f.path.casefold() for f in self.files]
        if len(set(paths))!=len(paths):raise MetadataInvalid()
        names=set(paths)
        if any('/'.join(p.split('/')[:n]) in names for p in paths for n in range(1,len(p.split('/')))):raise MetadataInvalid()
        if sum(f.size for f in self.files)>MAX_INTEGER:raise MetadataInvalid()
        integer(self.selected_index)
        videos=[f.index for f in self.files if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4')]
        if videos!=[self.selected_index] or self.files[self.selected_index].size<=0:raise MetadataInvalid()
        if len(self.json().encode('utf-8'))>1048576:raise MetadataInvalid()

    def json(self):
        return json.dumps({'files':[{'index':f.index,'path':f.path,'size':f.size} for f in self.files],
                           'selected_index':self.selected_index},sort_keys=True,separators=(',',':'))


@dataclass(frozen=True)
class BatchInventory:
    """Hash-bound file inventory, not a selected torrent or acquisition permit.

    Unlike Manifest, this type can represent multiple videos. No downstream
    single-video acquisition contract accepts it as a substitute for Manifest.
    """
    info_hash: str
    info_sha256: str
    files: tuple[TorrentFile,...] = field(repr=False)

    def __post_init__(self):
        if infohash(self.info_hash)!=self.info_hash:raise MetadataInvalid()
        if type(self.info_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',self.info_sha256):raise MetadataInvalid()
        if type(self.files) is not tuple or not 1<=len(self.files)<=10000:raise MetadataInvalid()
        if any(type(f) is not TorrentFile or f.index!=i for i,f in enumerate(self.files)):raise MetadataInvalid()
        paths=[f.path.casefold() for f in self.files]
        if len(set(paths))!=len(paths):raise MetadataInvalid()
        names=set(paths)
        if any('/'.join(p.split('/')[:n]) in names for p in paths for n in range(1,len(p.split('/')))):raise MetadataInvalid()
        if sum(f.size for f in self.files)>MAX_INTEGER:raise MetadataInvalid()
        videos=[f for f in self.files if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4','.m4v','.avi')]
        if not videos or any(f.size<=0 for f in videos):raise MetadataInvalid()
        allowed={'.mkv','.mp4','.m4v','.avi','.nfo','.srt','.ass','.ssa','.txt'}
        if any(PurePosixPath(f.path).suffix.lower() not in allowed for f in self.files):raise MetadataInvalid()
        if len(self.json().encode('utf-8'))>1048576:raise MetadataInvalid()

    def json(self):
        return json.dumps({'files':[{'index':f.index,'path':f.path,'size':f.size} for f in self.files]},
                          sort_keys=True,separators=(',',':'))


@dataclass(frozen=True)
class ObservedBatchInventory:
    """Read-only observed files from an owned, stopped qBittorrent batch.

    Deliberately has NO info_sha256: the old Web API does not expose the
    original bencoded info dictionary or its cryptographic digest. This is
    not a BatchInventory and cannot be used in existing BEP3-proof contracts.
    """
    info_hash: str
    files: tuple[TorrentFile, ...] = field(repr=False)

    def __post_init__(self):
        if infohash(self.info_hash)!=self.info_hash:raise MetadataInvalid()
        if type(self.files) is not tuple or not 1<=len(self.files)<=10000:raise MetadataInvalid()
        if any(type(f) is not TorrentFile or f.index!=i for i,f in enumerate(self.files)):
            raise MetadataInvalid()
        paths=[f.path.casefold() for f in self.files]
        if len(set(paths))!=len(paths):raise MetadataInvalid()
        names=set(paths)
        if any('/'.join(p.split('/')[:n]) in names for p in paths
               for n in range(1,len(p.split('/')))):raise MetadataInvalid()
        if sum(f.size for f in self.files)>MAX_INTEGER:raise MetadataInvalid()
        allowed={'.mkv','.mp4','.m4v','.avi','.nfo','.srt','.ass','.ssa','.txt'}
        if any(PurePosixPath(f.path).suffix.lower() not in allowed
               for f in self.files):raise MetadataInvalid()
        videos=[f for f in self.files
                if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4','.m4v','.avi')]
        if not videos or any(f.size<=0 for f in videos):raise MetadataInvalid()
        if len(self.json().encode('utf-8'))>1048576:raise MetadataInvalid()

    @property
    def info_sha256(self):
        # Deliberately NOT a BEP3 hash. Absence is persisted as SQL NULL.
        return None

    def json(self):
        return json.dumps({'files':[{'index':f.index,'path':f.path,'size':f.size}
                                    for f in self.files]},sort_keys=True,separators=(',',':'))


def read_batch_inventory(raw,expected_hash):
    """Typed observed batch evidence or independently hash-verified BEP3 bytes."""
    digest=infohash(expected_hash)
    if type(raw) is ObservedBatchInventory:
        if raw.info_hash!=digest:raise MetadataInvalid()
        return raw
    return parse_batch_metainfo(raw,digest)


@dataclass(frozen=True)
class ObservedInventory:
    """Stable, stopped qB file list; deliberately NOT BEP3 metainfo evidence.

    A verified source hash links the observed torrent, but no info SHA256 is
    fabricated. Transfer plans accept this distinct type only after durable
    ownership admission; it never authorizes a new torrent submission.
    """
    info_hash: str
    files: tuple[TorrentFile,...] = field(repr=False)
    selected_index: int

    def __post_init__(self):
        if infohash(self.info_hash)!=self.info_hash or type(self.files) is not tuple:
            raise MetadataInvalid()
        if not 1<=len(self.files)<=10000 or any(
                type(f) is not TorrentFile or f.index!=i
                for i,f in enumerate(self.files)):
            raise MetadataInvalid()
        names=[f.path.casefold() for f in self.files]
        if len(names)!=len(set(names)):
            raise MetadataInvalid()
        paths=set(names)
        if any('/'.join(p.split('/')[:n]) in paths
               for p in names for n in range(1,len(p.split('/')))):
            raise MetadataInvalid()
        if sum(f.size for f in self.files)>MAX_INTEGER:raise MetadataInvalid()
        allowed={'.mkv','.mp4','.nfo','.srt','.ass','.ssa','.txt'}
        if any(PurePosixPath(f.path).suffix.lower() not in allowed for f in self.files):
            raise MetadataInvalid()
        videos=[f.index for f in self.files
                if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4')]
        if type(self.selected_index) is not int or videos!=[self.selected_index]:
            raise MetadataInvalid()
        if self.files[self.selected_index].size<=0:raise MetadataInvalid()
        if len(self.json().encode('utf-8'))>1048576:raise MetadataInvalid()

    @property
    def info_sha256(self):
        """No metainfo bytes were observed; no cryptographic claim is made."""
        return None

    @classmethod
    def from_observation(cls,observation):
        from src.nextgen.acquisition.submission import Observation
        if type(observation) is not Observation or not observation.files:
            raise MetadataInvalid()
        videos=[f.index for f in observation.files
                if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4')]
        if len(videos)!=1:raise MetadataInvalid()
        return cls(observation.info_hash,observation.files,videos[0])

    def json(self):
        return json.dumps({
            'evidence':'qbittorrent_stopped_inventory','info_hash':self.info_hash,
            'files':[{'index':f.index,'path':f.path,'size':f.size}
                     for f in self.files],
            'selected_index':self.selected_index
        },sort_keys=True,separators=(',',':'))


class _Decoder:
    def __init__(self,raw):self.raw,self.pos,self.nodes,self.info_span=raw,0,0,None
    def string(self):
        end=self.raw.find(b':',self.pos,self.pos+10)
        if end<0:raise MetadataInvalid()
        length=self.raw[self.pos:end]
        if not re.fullmatch(b'0|[1-9][0-9]*',length):raise MetadataInvalid()
        count=int(length)
        self.pos=end+1
        if count>len(self.raw)-self.pos:raise MetadataInvalid()
        value=self.raw[self.pos:self.pos+count]
        self.pos+=count
        return value
    def value(self,depth=0):
        self.nodes+=1
        if self.nodes>100000 or depth>16 or self.pos>=len(self.raw):raise MetadataInvalid()
        tag=self.raw[self.pos:self.pos+1]
        if tag.isdigit():return self.string()
        self.pos+=1
        if tag==b'i':
            end=self.raw.find(b'e',self.pos,self.pos+22)
            if end<0:raise MetadataInvalid()
            raw=self.raw[self.pos:end]
            if not re.fullmatch(b'0|-?[1-9][0-9]*',raw):raise MetadataInvalid()
            value=int(raw)
            if not -MAX_INTEGER-1<=value<=MAX_INTEGER:raise MetadataInvalid()
            self.pos=end+1
            return value
        if tag not in (b'l',b'd'):raise MetadataInvalid()
        result=[] if tag==b'l' else {}
        previous=None
        while self.pos<len(self.raw) and self.raw[self.pos:self.pos+1]!=b'e':
            if tag==b'l':result.append(self.value(depth+1))
            else:
                key=self.string()
                if previous is not None and key<=previous:raise MetadataInvalid()
                previous=key
                start=self.pos
                result[key]=self.value(depth+1)
                if depth==0 and key==b'info':self.info_span=(start,self.pos)
        if self.pos>=len(self.raw):raise MetadataInvalid()
        self.pos+=1
        return result


def _parse_metainfo(raw,expected_hash,*,batch):
    """Metadata only: no file-content, audio, readiness or ownership assertion."""
    expected_hash=infohash(expected_hash)
    if type(raw) is not bytes or not 1<=len(raw)<=MAX_METAINFO:raise MetadataInvalid()
    decoder=_Decoder(raw)
    root=decoder.value()
    if type(root) is not dict or decoder.pos!=len(raw) or decoder.info_span is None:raise MetadataInvalid()
    start,end=decoder.info_span
    encoded=raw[start:end]
    if hashlib.sha1(encoded).hexdigest()!=expected_hash:raise MetadataInvalid()
    info=root[b'info']
    if type(info) is not dict or any(k in info for k in (b'meta version',b'file tree',b'attr',b'symlink path')):
        raise MetadataInvalid()
    if any(k.endswith(b'.utf-8') for k in info):raise MetadataInvalid()
    try:
        name=info[b'name'].decode('utf-8')
        safe_path(name)
        if '/' in name:raise MetadataInvalid()
        piece=info[b'piece length']
        pieces=info[b'pieces']
        if type(piece) is not int or not 1<=piece<=64*1024*1024 or type(pieces) is not bytes:raise MetadataInvalid()
        if (b'length' in info)==(b'files' in info):raise MetadataInvalid()
        rows=info.get(b'files')
        if rows is None:files=(TorrentFile(0,name,info[b'length']),)
        else:
            if type(rows) is not list or not 1<=len(rows)<=10000:raise MetadataInvalid()
            files=[]
            for index,row in enumerate(rows):
                if type(row) is not dict or any(k in row for k in (b'attr',b'symlink path')) or any(k.endswith(b'.utf-8') for k in row):raise MetadataInvalid()
                parts=row[b'path']
                if type(parts) is not list or not 1<=len(parts)<=16:raise MetadataInvalid()
                path=[]
                for part in parts:
                    text=part.decode('utf-8')
                    safe_path(text)
                    if '/' in text:raise MetadataInvalid()
                    path.append(text)
                files.append(TorrentFile(index,'/'.join([name,*path]),row[b'length']))
            files=tuple(files)
        total=sum(f.size for f in files)
        if total<=0 or len(pieces)!=20*((total+piece-1)//piece):raise MetadataInvalid()
        if batch:
            # A separate, non-authorizing inspection path for mixed packs.
            return BatchInventory(expected_hash,hashlib.sha256(encoded).hexdigest(),files)
        videos=[f.index for f in files if PurePosixPath(f.path).suffix.lower() in ('.mkv','.mp4')]
        if len(videos)!=1:raise MetadataInvalid()
        # Keep the production single-video contract unchanged until batch
        # verification, reservations and torrent selection are integrated.
        if any(PurePosixPath(f.path).suffix.lower() not in ('.mkv','.mp4','.nfo','.srt','.ass','.ssa','.txt') for f in files):raise MetadataInvalid()
        return Manifest(expected_hash,hashlib.sha256(encoded).hexdigest(),files,videos[0])
    except (KeyError,UnicodeError,AttributeError,IndexError,TypeError,ValueError):raise MetadataInvalid() from None


def parse_metainfo(raw,expected_hash):
    """Strict single-video acquisition path, preserved for existing jobs."""
    return _parse_metainfo(raw,expected_hash,batch=False)


def parse_batch_metainfo(raw,expected_hash):
    """Read-only, hash-verified multi-video inventory; selects no files."""
    return _parse_metainfo(raw,expected_hash,batch=True)
