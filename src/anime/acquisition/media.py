"""Read-only verification of one pinned selected file; never copies or deletes."""
import asyncio
import errno
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import selectors
import stat
import subprocess
import time

from src.anime.acquisition.cleanup import directory, parent
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


SAMPLE=65536


def content_id(fd, size):
    """A file's identity that touching it cannot change: its size and three samples of it.

    Owner, 2026-11-27: mtime moved on staged files nobody rewrote, and a remount changes
    device numbers, so neither may decide whether a file is still the one AKON recorded.
    SHA-256 of the size, the first, the middle and the last 64 KB: about 200 KB read on
    any size of video. A replaced, truncated or re-encoded file differs; a touched, copied
    or remounted one does not. Small files are read whole.
    """
    digest=hashlib.sha256(f'akon-content-1:{size}:'.encode())
    starts=(0,) if size<=3*SAMPLE else (0,size//2-SAMPLE//2,size-SAMPLE)
    for start in starts:
        os.lseek(fd,start,os.SEEK_SET)
        remaining=min(size-start,SAMPLE if size>3*SAMPLE else size)
        while remaining:
            chunk=os.read(fd,min(remaining,1024*1024))
            if not chunk:raise MediaInvalid()
            digest.update(chunk);remaining-=len(chunk)
    os.lseek(fd,0,os.SEEK_SET)
    return digest.hexdigest()


def path_content_id(root, relative, size):
    """content_id of root/relative, opened without following links; None when it is not there."""
    try:
        with directory(root) as base, parent(base, relative) as (folder, name):
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size!=size:return False
                return content_id(fd,size)
            finally:os.close(fd)
    except FileNotFoundError:
        return None


def same_content(root, relative, size, recorded):
    """True when the file is the one recorded: its size, and its content_id when one was
    recorded (rows from before content_id have only the size). None when it is not there."""
    found=path_content_id(root,relative,size)
    if found is None or found is False:return found
    return recorded is None or found==recorded


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
        '-show_entries','format=format_name,duration:stream=index,codec_type,codec_name,width,height,channels,bit_rate,profile:stream_tags=language,BPS,BPS-eng,title:stream_disposition=comment',
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


def verify_path(download_root, relative_path, size, flag, *, read_probe=probe, audio=JAPANESE_ENGLISH):
    """Check one downloaded video for the series job.

    The file is opened without following links, under the torrent's own folder;
    its size must be the torrent's; ffprobe reads its container and audio
    (acquisition.audio_check); the file must not change while it is read.
    Returns {'sha256', 'duration_ms', 'languages', 'root', 'file'}.
    """
    try:
        with directory(download_root) as root:
            root_sig=(os.fstat(root).st_dev,os.fstat(root).st_ino)
            with parent(root,relative_path) as (folder,name):
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
                try:
                    before=signature(os.fstat(fd))
                    if before[2]!=size:raise MediaInvalid()
                    media_hash=media_fingerprint(fd,size,before)
                    content=content_id(fd,size)
                    cancelled(flag)
                    duration,languages=parse_probe(read_probe(fd,flag),PurePosixPath(relative_path).suffix.lower(),audio)
                    cancelled(flag)
                    if signature(os.fstat(fd))!=before:raise MediaInvalid()
                finally:os.close(fd)
            with parent(root,relative_path) as (latest,leaf):
                if signature(os.stat(leaf,dir_fd=latest,follow_symlinks=False))!=before:raise MediaInvalid()
        return {'sha256':media_hash,'content':content,'duration_ms':duration,'languages':list(languages),
                'root':list(root_sig),'file':list(before)}
    except CancellationError:
        # The worker stopping: check again later. Otherwise the path left the folder.
        if flag.is_set():raise MediaUnavailable() from None
        raise MediaInvalid() from None
    except OSError as exc:
        # A link where the video or a folder should be is what was downloaded, not an
        # outage: waiting would never fix it. Anything else (the share away) is checked again.
        if exc.errno in (errno.ELOOP,errno.ENOTDIR):raise MediaInvalid() from None
        raise MediaUnavailable() from None
