"""Exact owned-torrent controls and fd-relative, manifest-only file cleanup."""
from contextlib import contextmanager
import errno
import os
import shutil
from pathlib import PurePosixPath
import stat
import threading

from src.anime.cancellation import CancellationError


class CleanupPermit:
    def __init__(self,fingerprint,action):
        self.fingerprint,self.action=fingerprint,action
        self._used=False;self._lock=threading.Lock()
    def take(self,fingerprint,action):
        with self._lock:
            if self._used or self.fingerprint!=fingerprint or self.action!=action:raise CancellationError()
            self._used=True


def owned(intent,observation):
    if observation is None:raise CancellationError()
    if (observation.info_hash!=intent.plan.manifest.info_hash or observation.files!=intent.plan.manifest.files
        or observation.save_path.rstrip('/')!=intent.options.download_root or observation.category!=intent.options.category
        or observation.auto_tmm is not False or 'ad-'+intent.plan.marker not in observation.tags
        or any(t.startswith('ad-') and t!='ad-'+intent.plan.marker for t in observation.tags)):raise CancellationError()
    return observation


@contextmanager
def directory(path):
    """Walk every absolute directory component without following symbolic links."""
    path=PurePosixPath(path)
    if not path.is_absolute() or '..' in path.parts:raise CancellationError()
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=new
        yield fd
    finally:os.close(fd)


@contextmanager
def parent(root_fd,relative):
    path=PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in ('.','..') for p in path.parts):raise CancellationError()
    fd=os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=new
        yield fd,path.name
    finally:os.close(fd)


def fingerprint(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise CancellationError()
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]


class FileCleanup:
    def __init__(self,intent,protected_roots):
        self.intent=intent
        self.root=intent.options.download_root
        path=PurePosixPath(self.root)
        if path.name!='ad-'+intent.plan.marker:raise CancellationError()
        for root in protected_roots:
            if not root:continue
            other=PurePosixPath(root)
            if not other.is_absolute() or path==other or path in other.parents or other in path.parents:raise CancellationError()
        self.expected={f.path:f.size for f in intent.plan.manifest.files}

    def _inventory(self,fd):
        result=set();count=0
        def walk(current,prefix,depth):
            nonlocal count
            if depth>18:raise CancellationError()
            with os.scandir(current) as entries:
                for entry in entries:
                    name=entry.name
                    count+=1
                    if count>20000:raise CancellationError()
                    path=prefix+name
                    info=os.stat(name,dir_fd=current,follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if not any(p.startswith(path+'/') for p in self.expected):raise CancellationError()
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=current)
                        try:walk(child,path+'/',depth+1)
                        finally:os.close(child)
                    else:
                        if path not in self.expected:raise CancellationError()
                        fingerprint(info);result.add(path)
        walk(fd,'',0)
        return result

    def capture(self):
        try:
            with directory(self.root) as fd:
                present=self._inventory(fd);result={}
                for path in sorted(present):
                    with parent(fd,path) as (directory_fd,name):
                        info=os.stat(name,dir_fd=directory_fd,follow_symlinks=False)
                        if info.st_size>self.expected[path]:raise CancellationError()
                        result[path]=fingerprint(info)
                return {'root':list((os.fstat(fd).st_dev,os.fstat(fd).st_ino)),'files':result}
        except FileNotFoundError:
            # Require an accessible local/shared payload root before authorizing
            # removal. A remote client's path is not proof of local file absence.
            raise CancellationError() from None

    def root_only(self):
        """Pin the owned ad-<marker> folder's identity; contents don't matter."""
        try:
            with directory(self.root) as fd:
                return {'root':list((os.fstat(fd).st_dev,os.fstat(fd).st_ino)),'files':{}}
        except FileNotFoundError:
            return {'root':None,'files':{}}

    def purge(self,plan):
        """Staging is confirmed and the torrent is gone from qBittorrent: delete
        the whole owned ad-<marker> folder, whatever is left in it. Only the
        exact pinned folder (no symlinks, same inode); rmtree is fd-based."""
        if type(plan) is not dict or plan.get('root') is None:return
        path=PurePosixPath(self.root)
        try:
            with directory(str(path.parent)) as above:
                info=os.stat(path.name,dir_fd=above,follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or [info.st_dev,info.st_ino]!=plan['root']:raise CancellationError()
                shutil.rmtree(path.name,dir_fd=above)
                os.fsync(above)
        except FileNotFoundError:
            return

    def _remove_empty(self,fd):
        # Called only after _inventory found no files; rmdir fails on content.
        with os.scandir(fd) as entries:names=[e.name for e in entries]
        for name in names:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            try:self._remove_empty(child)
            finally:os.close(child)
            os.rmdir(name,dir_fd=fd)
        os.fsync(fd)

    def remove(self,plan,*,guard=None):
        if type(plan) is not dict or set(plan)!={'root','files'} or type(plan['files']) is not dict:raise CancellationError()
        try:
            with directory(self.root) as fd:
                if plan['root']!=[os.fstat(fd).st_dev,os.fstat(fd).st_ino]:raise CancellationError()
                present=self._inventory(fd)
                if not present<=set(plan['files']):raise CancellationError()
                # Validate every remaining path before deleting any of them.
                for path in present:
                    with parent(fd,path) as (directory_fd,name):
                        if fingerprint(os.stat(name,dir_fd=directory_fd,follow_symlinks=False))!=plan['files'][path]:raise CancellationError()
                for path in sorted(present):
                    with parent(fd,path) as (directory_fd,name):
                        if fingerprint(os.stat(name,dir_fd=directory_fd,follow_symlinks=False))!=plan['files'][path]:raise CancellationError()
                        if guard is not None:guard()
                        os.unlink(name,dir_fd=directory_fd)
                        os.fsync(directory_fd)
                if self._inventory(fd):raise CancellationError()
                # Only empty folders remain: rmdir them bottom-up, then the
                # owned ad-<marker> root. rmdir never removes content.
                self._remove_empty(fd)
            name=PurePosixPath(self.root).name
            with directory(str(PurePosixPath(self.root).parent)) as above:
                try:os.rmdir(name,dir_fd=above);os.fsync(above)
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY,errno.EEXIST,errno.ENOENT):raise
        except FileNotFoundError:
            # A missing directory during traversal is ambiguous, not a blanket
            # success: verify the entire pinned root itself has disappeared.
            try:
                with directory(self.root):pass
            except FileNotFoundError:return
            raise CancellationError() from None
