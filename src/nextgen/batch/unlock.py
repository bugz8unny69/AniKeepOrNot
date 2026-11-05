"""Bounded audio-gated planning for exactly verified batch trial files.

The probe file must have already passed immutable JP+EN media verification.
These functions are pure: no database, filesystem or torrent client writes.
"""
from dataclasses import dataclass
import hashlib
import json

from src.nextgen.acquisition.media import VerifiedMedia
from src.nextgen.acquisition.transfers import (
    TransferSnapshot, TransferConflict,
)
from src.nextgen.batch.probe_transfer import verify_probe_snapshot
from src.nextgen.storage.batch_submission_journal import BatchSubmissionIntent


@dataclass(frozen=True)
class BatchUnlockIntent:
    plan_id: int
    fingerprint: str
    priorities: tuple[int,...]
    piece_size: int
    verification_job_id: int


def planned_priorities(intent):
    if type(intent) is not BatchSubmissionIntent:raise TransferConflict()
    indices=[index for _,index in intent.candidate.planned_episodes]
    size=len(intent.inventory.files)
    if (len(indices)!=len(set(indices)) or not indices
            or any(type(i) is not int or not 0<=i<size for i in indices)
            or intent.candidate.probe_index not in indices):
        raise TransferConflict()
    wanted=set(indices)
    return tuple(1 if i in wanted else 0 for i in range(size))


def validate_unlock_snapshot(intent,snapshot,*,stopped=False):
    if type(intent) is not BatchSubmissionIntent or type(snapshot) is not TransferSnapshot:
        raise TransferConflict()
    # Reuse canonical ownership, ordered file-size and piece-range evidence.
    verify_probe_snapshot(intent,snapshot,require_stopped=stopped)
    return snapshot


def plan_verified_unlock(intent,snapshot,media,verification_job_id):
    if (type(media) is not VerifiedMedia or type(verification_job_id) is not int
            or verification_job_id<1):
        raise TransferConflict()
    if (media.plan_id!=intent.candidate.plan_id
            or media.preparation_job_id!=intent.candidate.reservation_job_id
            or media.submission_fingerprint!=intent.fingerprint
            or media.relative_path!=intent.inventory.files[intent.candidate.probe_index].path):
        raise TransferConflict()
    validate_unlock_snapshot(intent,snapshot,stopped=True)
    priorities=planned_priorities(intent)
    probe=intent.candidate.probe_index
    if snapshot.priorities!=tuple(1 if i==probe else 0
                                 for i in range(len(intent.inventory.files))):
        raise TransferConflict()
    if not any(p==1 and i!=probe for i,p in enumerate(priorities)):
        raise TransferConflict()
    fingerprint=unlock_fingerprint(intent,media,snapshot.piece_size)
    return BatchUnlockIntent(intent.candidate.plan_id,fingerprint,priorities,
                             snapshot.piece_size,verification_job_id)


def unlock_fingerprint(intent,media,piece_size):
    if (type(intent) is not BatchSubmissionIntent or type(media) is not VerifiedMedia
            or type(piece_size) is not int or not 1<=piece_size<=67108864):
        raise TransferConflict()
    return hashlib.sha256(json.dumps({
        'submission':intent.fingerprint,
        'media_sha256':media.sha256,
        'media_proof':hashlib.sha256(media.json().encode('utf-8')).hexdigest(),
        'priorities':planned_priorities(intent),
        'piece_size':piece_size,
        'overlap_bytes':0,
    },sort_keys=True,separators=(',',':')).encode()).hexdigest()


def exact_unlock_selection(plan,snapshot):
    if type(plan) is not BatchUnlockIntent or type(snapshot) is not TransferSnapshot:
        raise TransferConflict()
    return snapshot.piece_size==plan.piece_size and snapshot.priorities==plan.priorities
