"""Read-only verification of one pinned selected file; never copies or deletes."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import selectors
import stat
import subprocess
import time

from src.anime.acquisition.cleanup import directory, parent, FileCleanup
from src.anime.cancellation import CancellationError
from src.anime.acquisition.audio import JAPANESE_ENGLISH, required_languages

MAX_PROBE_BYTES=1024*1024
# Recorded Japanese/English tracks found; empty when the audio check is off.
LANGUAGE_SETS=((),('jpn',),('eng',),('eng','jpn'))


class MediaInvalid(ValueError):
    reason_code='MEDIA_VERIFICATION_INVALID'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The downloaded video needs verification review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class MediaUnavailable(RuntimeError):
    reason_code='MEDIA_VERIFICATION_UNAVAILABLE'
    def __init__(self):super().__init__('The downloaded video could not be checked yet.')


class ProbeUnavailable(RuntimeError):
    reason_code='MEDIA_PROBE_UNAVAILABLE'
    def __init__(self):super().__init__('The local media probe is unavailable.')


class AudioMissing(MediaInvalid):
    reason_code='MEDIA_AUDIO_REQUIRED'
    def __init__(self,required=('eng','jpn'),observed=(),missing=None):
        self.required=tuple(sorted(required))
        self.observed=tuple(sorted(observed))
        self.missing=tuple(sorted(missing if missing is not None else (set(self.required)-set(self.observed))))
        names={'jpn':'Japanese','eng':'English'}
        wanted=' and '.join(names.get(x,x) for x in ('jpn','eng') if x in self.required)
        ValueError.__init__(self,wanted+' audio '+('tracks are' if len(self.required)>1 else 'track is')+' required.')


def signature(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise MediaInvalid()
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def media_fingerprint(fd, size, before, *, sample_size=65536):
    """Compute a lightweight media fingerprint from the container header and file attributes.

    Avoids sequentially streaming multi-gigabyte video payloads from disk while
    deterministically binding to the container's initial structure, file size,
    and modification timestamp.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    to_read = min(size, sample_size)
    header = bytearray()
    while len(header) < to_read:
        chunk = os.read(fd, to_read - len(header))
        if not chunk:
            raise MediaInvalid()
        header.extend(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256(header)
    if size > sample_size:
        digest.update(f":{size}:{before[3]}".encode())
    return digest.hexdigest()


def cancelled(flag):
    if flag.is_set():raise asyncio.CancelledError()


def probe(fd,flag,*,executable='/usr/bin/ffprobe',timeout=60):
    """Linux fd input; fixed arguments, no shell/network/playlist demuxers.

    Executable/timeout overrides are for trusted callers and offline doubles,
    never input from a release. Output is capped while the process is running.
    """
    cancelled(flag)
    command=[executable,'-v','error','-max_alloc','67108864',
        '-protocol_whitelist','file,pipe','-format_whitelist','matroska,webm,mov,mp4,m4a,3gp,3g2,mj2',
        '-probesize','10000000','-analyzeduration','10000000',
        '-show_entries','format=format_name,duration:stream=index,codec_type,codec_name,width,height:stream_tags=language:stream_disposition=comment',
        '-of','json','/proc/self/fd/'+str(fd)]
    try:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,pass_fds=(fd,),close_fds=True)
    except OSError:raise ProbeUnavailable() from None
    deadline=time.monotonic()+timeout
    output=bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while True:
                cancelled(flag)
                if time.monotonic()>=deadline:raise MediaUnavailable()
                if not selector.select(.1):continue
                chunk=os.read(process.stdout.fileno(),min(65536,MAX_PROBE_BYTES+1-len(output)))
                if not chunk:break
                output.extend(chunk)
                if len(output)>MAX_PROBE_BYTES:raise MediaInvalid()
        while process.poll() is None:
            cancelled(flag)
            if time.monotonic()>=deadline:raise MediaUnavailable()
            try:process.wait(timeout=.1)
            except subprocess.TimeoutExpired:pass
        if process.returncode!=0:raise MediaInvalid()
        return bytes(output)
    finally:
        if process.poll() is None:process.kill()
        process.wait()
        process.stdout.close()


def parse_probe(raw,suffix,audio=JAPANESE_ENGLISH):
    """audio: acquisition.audio_check. Extra audio tracks never fail the check."""
    required=required_languages(audio)
    if type(raw) is not bytes or not 1<=len(raw)<=MAX_PROBE_BYTES:raise MediaInvalid()
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise MediaInvalid()
            result[key]=value
        return result
    try:
        document=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:None)
        if type(document) is not dict:raise MediaInvalid()
        info=document['format'];streams=document['streams']
        if type(info) is not dict or type(streams) is not list or not 1<=len(streams)<=128:raise MediaInvalid()
        formats=info['format_name'].split(',')
        if (suffix=='.mkv' and 'matroska' not in formats) or (suffix=='.mp4' and 'mp4' not in formats):raise MediaInvalid()
        duration=float(info['duration'])
        if not math.isfinite(duration) or not 0<duration<=604800:raise MediaInvalid()
        videos=0;languages=set();indices=set();all_observed=set()
        for stream in streams:
            if type(stream) is not dict:raise MediaInvalid()
            index=stream['index']
            if type(index) is not int or index<0 or index in indices:raise MediaInvalid()
            indices.add(index)
            codec=stream.get('codec_name')
            if stream.get('codec_type') in ('video','audio') and (type(codec) is not str or not codec or codec=='unknown'):
                raise MediaInvalid()
            if stream.get('codec_type')=='video':
                width,height=stream.get('width'),stream.get('height')
                if type(width) is not int or type(height) is not int or not 1<=width<=32768 or not 1<=height<=32768:raise MediaInvalid()
                videos+=1
            if stream.get('codec_type')=='audio':
                tags=stream.get('tags',{});disposition=stream.get('disposition',{})
                if type(tags) is not dict or type(disposition) is not dict:raise MediaInvalid()
                language=tags.get('language','')
                if type(language) is not str:raise MediaInvalid()
                if disposition.get('comment',0)!=0:continue
                raw_lang=language.lower().strip()
                norm_lang={'ja':'jpn','en':'eng'}.get(raw_lang,raw_lang)
                tag=norm_lang if norm_lang else 'und'
                all_observed.add(tag)
                if norm_lang in ('jpn','eng'):languages.add(norm_lang)
        if videos!=1:raise MediaInvalid()
        if not required<=languages:
            raise AudioMissing(tuple(sorted(required)),tuple(sorted(all_observed)),tuple(sorted(required-languages)))
        return max(1,round(duration*1000)),tuple(sorted(languages))
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError) as exc:
        if isinstance(exc,MediaInvalid):raise
        raise MediaInvalid() from None


@dataclass(frozen=True,repr=False)
class VerifiedMedia:
    preparation_job_id: int
    download_job_id: int
    plan_id: int
    submission_fingerprint: str
    transfer_fingerprint: str
    relative_path: str
    root_signature: tuple
    file_signature: tuple
    sha256: str
    duration_ms: int
    languages: tuple

    def __post_init__(self):
        from src.anime.history import integer
        from src.anime.acquisition.metadata import safe_path
        for value in (self.preparation_job_id,self.download_job_id,self.plan_id,self.duration_ms):integer(value,positive=True)
        import re
        for value in (self.submission_fingerprint,self.transfer_fingerprint,self.sha256):
            if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise MediaInvalid()
        safe_path(self.relative_path)
        if (type(self.root_signature) is not tuple or len(self.root_signature)!=2
                or type(self.file_signature) is not tuple or len(self.file_signature)!=5
                or any(type(x) is not int or x<0 for x in self.root_signature+self.file_signature)
                or self.file_signature[2]<=0 or self.languages not in LANGUAGE_SETS):raise MediaInvalid()

    def json(self):
        return json.dumps({'path':self.relative_path,'root':self.root_signature,'file':self.file_signature,
            'sha256':self.sha256,'duration_ms':self.duration_ms,'languages':self.languages},sort_keys=True,separators=(',',':'))


def verify_file(intent,record,download_job_id,flag,*,read_probe=probe,protected_roots=(),audio=JAPANESE_ENGLISH):
    """Authoritative lightweight media verification for single acquisitions.

    Trusts the completed download transfer, verifies expected file size and container
    integrity via a single ffprobe inspection of audio streams, and computes a lightweight
    media fingerprint (container header + attributes) without sequential full-payload streaming.
    """
    selected=intent.plan.manifest.files[intent.plan.manifest.selected_index]
    # Same exact owned download root and unknown-file rejection as scoped cleanup.
    try:
        cleanup=FileCleanup(intent,protected_roots)
        cancelled(flag)
        inventory=cleanup.capture()
        with directory(intent.options.download_root) as root:
            root_sig=(os.fstat(root).st_dev,os.fstat(root).st_ino)
            if list(root_sig)!=inventory['root']:raise MediaInvalid()
            with parent(root,selected.path) as (folder,name):
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
                try:
                    before=signature(os.fstat(fd))
                    if before[2]!=selected.size:raise MediaInvalid()
                    media_hash=media_fingerprint(fd,selected.size,before)
                    cancelled(flag)
                    duration,languages=parse_probe(read_probe(fd,flag),PurePosixPath(selected.path).suffix.lower(),audio)
                    cancelled(flag)
                    if signature(os.fstat(fd))!=before:raise MediaInvalid()
                    # Rewalk the absolute path: renamed roots/parents cannot pass
                    # solely because the original fd still addresses the old inode.
                    with directory(intent.options.download_root) as current:
                        if (os.fstat(current).st_dev,os.fstat(current).st_ino)!=root_sig:raise MediaInvalid()
                        with parent(current,selected.path) as (latest,leaf):
                            if signature(os.stat(leaf,dir_fd=latest,follow_symlinks=False))!=before:raise MediaInvalid()
                    return VerifiedMedia(intent.plan.preparation_job_id,download_job_id,intent.plan.plan_id,
                        intent.fingerprint,record.fingerprint,selected.path,root_sig,before,media_hash,duration,languages)
                finally:os.close(fd)
    except CancellationError:raise MediaInvalid() from None
    except OSError:raise MediaUnavailable() from None
