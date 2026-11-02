"""Journaled viewing-copy publication with tracked partial files; no refresh or Keep."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import xml.etree.ElementTree as ET

from src.nextgen.acquisition.cleanup import directory, parent
from src.nextgen.acquisition.media import signature, cancelled
from src.nextgen.cancellation import CancellationError


class StagingReview(ValueError):
    reason_code='STAGING_REVIEW'
    def __init__(self):super().__init__('Staging needs review; existing files have been preserved.')


class StagingBusy(RuntimeError):
    reason_code='STAGING_BUSY'
    def __init__(self):super().__init__('Another staging attempt is still draining.')


class StagingUnavailable(RuntimeError):
    reason_code='STAGING_UNAVAILABLE'
    def __init__(self):super().__init__('The staging filesystem is temporarily unavailable.')


class StagingConfiguration(StagingReview):
    reason_code='STAGING_CONFIGURATION_INVALID'
    def __init__(self):ValueError.__init__(self,'Configure an accessible separate staging folder and a non-root worker.')


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def slug(title):
    # Provider titles remain XML text; paths use a bounded portable projection.
    text=re.sub(r'[^\w .-]','_',title,flags=re.UNICODE).strip(' .')
    while len(text.encode('utf-8'))>120:text=text[:-1]
    return text or 'Anime'


def nfo(root,values,ids):
    document=ET.Element(root)
    for key,value in values:ET.SubElement(document,key).text=str(value)
    for provider,identity in ids:
        ET.SubElement(document,'uniqueid',{'type':provider,'default':'true' if provider=='tvdb' else 'false'}).text=str(identity)
    return ET.tostring(document,encoding='utf-8',xml_declaration=True)


def specification(intent,evidence,identity,root,revision,*,display_title=None):
    """Pure deterministic layout from verified placement, never release paths."""
    anime,episode,series,season,number,tvdb_episode,title=identity
    from src.nextgen.history import integer
    for value in (anime,episode,series,number,tvdb_episode,revision):integer(value,positive=True)
    integer(season)
    folder=f'{slug(title)} [tvdbid-{series}] [anidbid-{anime}]'
    season_folder=folder+f'/Season {season:02d}'
    basename=f'{slug(title)} - S{season:02d}E{number:02d}'
    media=season_folder+'/'+basename+PurePosixPath(evidence['path']).suffix.lower()
    # Display metadata can differ from the canonical filesystem naming.
    shown = display_title or title
    show=nfo('tvshow',[('title',shown)],[('tvdb',series),('anidb',anime)])
    episode_nfo=nfo('episodedetails',[('title',f'Episode {number}'),('showtitle',shown),('season',season),('episode',number)],
                    [('tvdb',tvdb_episode),('anidb',episode)])
    entries=[{'role':'media','path':media,'sha256':evidence['sha256'],'size':evidence['file'][2],'text':None},
             {'role':'episode','path':season_folder+'/'+basename+'.nfo','sha256':hashlib.sha256(episode_nfo).hexdigest(),
              'size':len(episode_nfo),'text':episode_nfo.decode()},
             {'role':'series','path':folder+'/tvshow.nfo','sha256':hashlib.sha256(show).hexdigest(),
              'size':len(show),'text':show.decode()}]
    # Each name is minted once and stored before any file creation.
    import uuid
    for entry in entries:entry['temporary']='.ad-'+uuid.uuid4().hex+'.partial'
    return {'root':root,'revision':revision,'source_root':intent.options.download_root,
            'source':evidence,'entries':entries,'file_mode':0o644,'directory_mode':0o755,'transfer':'MOVE'}


def root_identity(root):
    with directory(root) as fd:
        info=os.fstat(fd)
        return [info.st_dev,info.st_ino]


def disjoint(root,others):
    path=PurePosixPath(root)
    if not root or not path.is_absolute() or root=='/' or str(path)!=root or '..' in path.parts:raise StagingConfiguration()
    for other in others:
        if not other:continue
        other=PurePosixPath(other)
        if path==other or path in other.parents or other in path.parents:raise StagingConfiguration()


@contextmanager
def destination(root,relative,expected_root,mode,*,lock=False):
    """Create only deterministic subdirectories, no symlink traversal or repair."""
    with directory(root) as base:
        current=os.dup(base)
        try:
            for part in PurePosixPath(relative).parts:
                if part in ('.','..','/') or not part:raise StagingReview()
                try:
                    os.mkdir(part,mode=mode,dir_fd=current);os.fsync(current)
                except FileExistsError:pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=current)
                os.close(current);current=child
            if lock:
                try:fcntl.flock(current,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise StagingBusy() from None
            yield current
        finally:os.close(current)


def digest_fd(fd,size,flag):
    before=signature(os.fstat(fd))
    if before[2]!=size:raise StagingReview()
    os.lseek(fd,0,os.SEEK_SET);remaining=size;digest=hashlib.sha256()
    while remaining:
        cancelled(flag);chunk=os.read(fd,min(1024*1024,remaining))
        if not chunk:raise StagingReview()
        digest.update(chunk);remaining-=len(chunk)
    if os.read(fd,1) or signature(os.fstat(fd))!=before:raise StagingReview()
    return digest.hexdigest(),before


def open_regular(folder,name,flags=os.O_RDONLY):
    fd=os.open(name,flags|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
    try:signature(os.fstat(fd))
    except BaseException:os.close(fd);raise
    return fd


def rename_file(folder,source,target,*,target_folder=None):
    # Cross-filesystem data is already copied into the tracked destination
    # partial. Publishing it only needs a normal rename within that directory.
    os.rename(source,target,src_dir_fd=folder,
              dst_dir_fd=folder if target_folder is None else target_folder)


class Publisher:
    def __init__(self,journal,claim,flag,*,allow_root=False):
        if os.geteuid()==0 and not allow_root:raise StagingConfiguration()
        self.journal,self.claim,self.flag=journal,claim,flag

    def guard(self):
        cancelled(self.flag);self.journal.guard(self.claim)

    def _source(self,plan):
        from contextlib import ExitStack
        stack=ExitStack()
        try:
            root=stack.enter_context(directory(plan['source_root']))
            folder,name=stack.enter_context(parent(root,plan['source']['path']))
            fd=open_regular(folder,name);stack.callback(os.close,fd)
            info=signature(os.fstat(fd))
            # Verification already hashed this video. Its saved device/inode
            # are not durable across NFS remounts; size and mtime are cheap
            # content-change indicators for the subsequent staging attempt.
            if info[2:4]!=tuple(plan['source']['file'][2:4]):raise StagingReview()
            return stack,fd
        except BaseException:stack.close();raise

    def _check(self,entry,fd):
        if entry['role']!='media':return digest_fd(fd,entry['size'],self.flag)
        info=signature(os.fstat(fd))
        if info[2]!=entry['size']:raise StagingReview()
        if getattr(self,'_copy_mode',False):
            return digest_fd(fd,entry['size'],self.flag)
        # MOVE reuses the digest established by media verification.
        return entry['sha256'],info

    def _remove_source(self,plan):
        self.guard()
        with directory(plan['source_root']) as root:
            with parent(root,plan['source']['path']) as (folder,name):
                try:info=signature(os.stat(name,dir_fd=folder,follow_symlinks=False))
                except FileNotFoundError:return
                # The file's saved size and mtime are sufficient here;
                # ownership is established by its verified source path.
                if info[2:4]!=tuple(plan['source']['file'][2:4]):raise StagingReview()
                self.guard();os.unlink(name,dir_fd=folder);os.fsync(folder)

    def _copy(self,plan,entry,fd):
        if entry['text'] is not None:
            data=entry['text'].encode();self.guard();os.ftruncate(fd,0);os.lseek(fd,0,os.SEEK_SET)
            view=memoryview(data)
            while view:
                self.guard();count=os.write(fd,view)
                if count<=0:raise StagingUnavailable()
                view=view[count:]
        else:
            stack,source=self._source(plan)
            with stack:
                self.guard();os.ftruncate(fd,0);os.lseek(fd,0,os.SEEK_SET)
                remaining=entry['size']
                while remaining:
                    self.guard();chunk=os.read(source,min(1024*1024,remaining))
                    if not chunk:raise StagingReview()
                    remaining-=len(chunk);view=memoryview(chunk)
                    while view:
                        count=os.write(fd,view)
                        if count<=0:raise StagingUnavailable()
                        view=view[count:]
                if os.read(source,1):raise StagingReview()
                if signature(os.fstat(source))[2:4]!=tuple(plan['source']['file'][2:4]):raise StagingReview()
                # Also prove the current absolute source path still resolves to it.
                latest,latest_fd=self._source(plan)
                with latest:
                    if signature(os.fstat(latest_fd))[2:4]!=signature(os.fstat(source))[2:4]:raise StagingReview()
        os.fchmod(fd,plan['file_mode']);os.fsync(fd)

    def publish(self,plan):
        if plan.get('transfer') not in ('MOVE','COPY'):raise StagingReview()
        self._copy_mode=plan['transfer']=='COPY'
        receipts=[]
        for entry in plan['entries']:
            self.guard()
            relative=PurePosixPath(entry['path']);folder_relative=str(relative.parent)
            root=plan['root']+'/'+folder_relative
            with destination(plan['root'],folder_relative,plan['root_identity'],plan['directory_mode'],lock=entry['role']=='series') as folder:
                folder_sig=[os.fstat(folder).st_dev,os.fstat(folder).st_ino]
                artifact=self.journal.artifact(self.claim,root,entry['temporary'])
                try:final=open_regular(folder,relative.name)
                except FileNotFoundError:final=None
                if final is not None:
                    try:
                        digest,info=self._check(entry,final)
                        if digest!=entry['sha256']:raise StagingReview()
                        # Shared series NFO can be reused only with exact content.
                        # Media/episode files require our own publication intent.
                        if artifact is None:
                            if entry['role']!='series':raise StagingReview()
                        elif artifact['phase'] not in ('PUBLISH_UNCERTAIN','PUBLISHED'):
                            raise StagingReview()
                        if artifact is not None:
                            if artifact['phase']=='PUBLISH_UNCERTAIN':self.journal.confirm(self.claim,artifact['id'])
                        os.fsync(final);os.fsync(folder)
                        receipts.append({'role':entry['role'],'path':entry['path'],'root':folder_sig,
                            'file':list(info),'sha256':digest,'hash_checked':entry['role']!='media' or self._copy_mode,'artifact':None if artifact is None else artifact['id']})
                    finally:os.close(final)
                    continue
                if artifact is not None and artifact['phase'] not in ('TEMP','PUBLISH_UNCERTAIN'):raise StagingReview()
                moved=False
                if entry['role']=='media' and artifact is None and not self._copy_mode:
                    stack,source=self._source(plan)
                    with stack:
                        source_info=signature(os.fstat(source))
                        if source_info[0]==folder_sig[0]:
                            # Journal the exact source inode at the destination
                            # before a same-filesystem rename can move it.
                            artifact=self.journal.register_move(self.claim,root,entry['temporary'],source_info)
                if artifact is None:
                    fd=os.open(entry['temporary'],os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=folder)
                    try:
                        os.fsync(fd);os.fsync(folder)
                        artifact=self.journal.register(self.claim,root,entry['temporary'])
                    except BaseException:os.close(fd);raise
                else:
                    same_source=entry['role']=='media' and artifact['file']==plan['source']['file'][:2]
                    try:fd=open_regular(folder,entry['temporary'],os.O_RDONLY if same_source else os.O_RDWR)
                    except FileNotFoundError:
                        # A journaled same-filesystem move can have a changed
                        # NFS inode after a remount. With the temporary absent,
                        # recheck the verified source by path and current device.
                        if entry['role']!='media' or self._copy_mode or artifact['phase']!='TEMP':
                            raise StagingReview()
                        self.guard()
                        with directory(plan['source_root']) as source_root:
                            with parent(source_root,plan['source']['path']) as (source_folder,name):
                                source_info=signature(os.stat(name,dir_fd=source_folder,follow_symlinks=False))
                                if (source_info[2:4]!=tuple(plan['source']['file'][2:4])
                                        or source_info[0]!=folder_sig[0]):raise StagingReview()
                                rename_file(source_folder,name,entry['temporary'],target_folder=folder)
                                os.fsync(folder);os.fsync(source_folder)
                        fd=open_regular(folder,entry['temporary'])
                        same_source=True
                    moved=same_source
                try:
                    if artifact['phase']=='TEMP' and not moved:self._copy(plan,entry,fd)
                    elif moved:os.fsync(fd)
                    digest,before=self._check(entry,fd)
                    if digest!=entry['sha256']:raise StagingReview()
                    if artifact['phase']=='TEMP':self.journal.intent(self.claim,artifact['id'])
                    self.guard()
                    # No durable inode dependency: verify the current named
                    # temporary file still has the size/mtime just checked.
                    if signature(os.stat(entry['temporary'],dir_fd=folder,follow_symlinks=False))[2:4]!=before[2:4]:raise StagingReview()
                    rename_file(folder,entry['temporary'],relative.name);os.fsync(folder)
                    after=signature(os.stat(relative.name,dir_fd=folder,follow_symlinks=False))
                    if after[2:4]!=before[2:4]:raise StagingReview()
                    self.journal.confirm(self.claim,artifact['id'])
                    receipts.append({'role':entry['role'],'path':entry['path'],'root':folder_sig,
                        'file':list(after),'sha256':digest,'hash_checked':entry['role']!='media' or self._copy_mode,'artifact':artifact['id']})
                finally:os.close(fd)
        # Revalidate all published files, including those recovered from an old
        # attempt. No source deletion or broad cleanup is part of staging.
        for receipt,entry in zip(receipts,plan['entries']):
            self.guard()
            path=PurePosixPath(entry['path'])
            with directory(plan['root']) as base:
                with parent(base,entry['path']) as (folder,name):
                    fd=open_regular(folder,name)
                    try:
                        digest,info=self._check(entry,fd)
                        if digest!=receipt['sha256'] or info[2:4]!=tuple(receipt['file'][2:4]):raise StagingReview()
                    finally:os.close(fd)
        # Batch qBittorrent files are shared torrent-owned sources; staging
        # publishes an independently hashed COPY and never unlinks/renames them.
        if not self._copy_mode:self._remove_source(plan)
        return receipts
