"""Read-only E01 media/audio evidence for a completed, owned batch probe.

This does NOT stage a file or allow subsequent files to download. It uses
fd-relative, no-follow traversal and the same bounded ffprobe parser as the
single-episode verifier, without pretending a pack is a single Manifest.
"""
import os
import json
from pathlib import PurePosixPath

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.media import (
    MediaInvalid, MediaUnavailable, VerifiedMedia, cancelled,
    signature, probe, parse_probe, media_fingerprint,
)
from src.anime.acquisition.audio import JAPANESE_ENGLISH
from src.anime.batch.probe_transfer import selection_fingerprint
from src.anime.storage.batch_submission_journal import BatchSubmissionIntent
from src.anime.storage.batch_transfer_journal import BatchTransferRecord


def verify_batch_probe_file(intent,record,transfer_job_id,flag,*,
                            read_probe=probe,protected_roots=(),audio=JAPANESE_ENGLISH):
    """Authoritative lightweight media verification for the batch probe file.

    Validates completed transfer, checks file size and audio tracks, and computes
    lightweight container fingerprint without sequential full-payload streaming.
    """
    if (type(intent) is not BatchSubmissionIntent
            or type(record) is not BatchTransferRecord
            or record.phase!='COMPLETE' or record.progress!=1
            or record.fingerprint!=selection_fingerprint(intent,record.piece_size)
            or record.selected_index!=intent.candidate.probe_index
            or record.transfer_job_id!=transfer_job_id):
        raise MediaInvalid()
    selected=intent.inventory.files[intent.candidate.probe_index]
    if PurePosixPath(selected.path).suffix.lower() not in ('.mkv','.mp4'):
        raise MediaInvalid()
    root=intent.options.download_root
    from src.anime.staging import disjoint, StagingConfiguration
    try:disjoint(root,protected_roots)
    except StagingConfiguration:raise MediaInvalid() from None
    try:
        cancelled(flag)
        with directory(root) as directory_fd:
            st=os.fstat(directory_fd)
            root_sig=(st.st_dev,st.st_ino)
            with parent(directory_fd,selected.path) as (folder,name):
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=folder)
                try:
                    before=signature(os.fstat(fd))
                    if before[2]!=selected.size:raise MediaInvalid()
                    media_hash=media_fingerprint(fd,selected.size,before)
                    cancelled(flag)
                    duration,languages=parse_probe(
                        read_probe(fd,flag),PurePosixPath(selected.path).suffix.lower(),audio)
                    cancelled(flag)
                    if signature(os.fstat(fd))!=before:raise MediaInvalid()
                    with directory(root) as current:
                        if (os.fstat(current).st_dev,os.fstat(current).st_ino)!=root_sig:
                            raise MediaInvalid()
                        with parent(current,selected.path) as (latest,leaf):
                            if signature(os.stat(leaf,dir_fd=latest,follow_symlinks=False))!=before:
                                raise MediaInvalid()
                    return VerifiedMedia(
                        intent.candidate.reservation_job_id,transfer_job_id,
                        intent.candidate.plan_id,intent.fingerprint,record.fingerprint,
                        selected.path,root_sig,before,media_hash,duration,languages)
                finally:
                    os.close(fd)
    except OSError:
        raise MediaUnavailable() from None


def verify_batch_episode_file(intent,record,unlock_job_id,file_index,flag,*,
                              read_probe=probe,protected_roots=(),audio=JAPANESE_ENGLISH):
    """Authoritative lightweight media verification for one planned batch episode."""
    from src.anime.storage.batch_unlock_journal import BatchUnlockRecord
    from src.anime.batch.unlock import planned_priorities
    if (type(intent) is not BatchSubmissionIntent
            or type(record) is not BatchUnlockRecord
            or record.phase!='COMPLETE'
            or record.first_job_id!=unlock_job_id
            or record.intent.plan_id!=intent.candidate.plan_id
            or record.intent.priorities!=planned_priorities(intent)
            or type(file_index) is not int
            or file_index not in {i for _,i in intent.candidate.planned_episodes}):
        raise MediaInvalid()
    selected=intent.inventory.files[file_index]
    suffix=PurePosixPath(selected.path).suffix.lower()
    if suffix not in ('.mkv','.mp4'):raise MediaInvalid()
    from src.anime.staging import disjoint, StagingConfiguration
    try:disjoint(intent.options.download_root,protected_roots)
    except StagingConfiguration:raise MediaInvalid() from None
    root=intent.options.download_root
    try:
        cancelled(flag)
        with directory(root) as base:
            st=os.fstat(base)
            root_sig=(st.st_dev,st.st_ino)
            with parent(base,selected.path) as (folder,name):
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|
                           os.O_CLOEXEC,dir_fd=folder)
                try:
                    before=signature(os.fstat(fd))
                    if before[2]!=selected.size:raise MediaInvalid()
                    media_hash=media_fingerprint(fd,selected.size,before)
                    cancelled(flag)
                    raw_probe=read_probe(fd,flag)
                    duration,languages=parse_probe(raw_probe,suffix,audio)
                    cancelled(flag)
                    if signature(os.fstat(fd))!=before:raise MediaInvalid()
                    with directory(root) as fresh:
                        if (os.fstat(fresh).st_dev,os.fstat(fresh).st_ino)!=root_sig:
                            raise MediaInvalid()
                        with parent(fresh,selected.path) as (latest,leaf):
                            if signature(os.stat(
                                    leaf,dir_fd=latest,follow_symlinks=False))!=before:
                                raise MediaInvalid()
                    return VerifiedMedia(
                        intent.candidate.reservation_job_id,unlock_job_id,
                        intent.candidate.plan_id,intent.fingerprint,
                        record.intent.fingerprint,selected.path,root_sig,before,
                        media_hash,duration,languages,probe_info=json.loads(raw_probe))
                finally:os.close(fd)
    except OSError:raise MediaUnavailable() from None
