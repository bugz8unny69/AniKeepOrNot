"""Symlink-free, fd-relative walks to a folder and to a file's parent folder."""
from contextlib import contextmanager
import os
from pathlib import PurePosixPath

from src.anime.cancellation import CancellationError


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

