"""Pure, bounded, fail-closed verification for E01-only batch probe selection.

No torrent client or filesystem writes. The metadata file order, exact
ownership, reported canonical piece ranges and all priorities must agree.
"""
import hashlib
import json

from src.nextgen.acquisition.transfers import (
    TransferSnapshot, TransferConflict, STOPPED, CHECKING, ACTIVE,
)
from src.nextgen.storage.batch_submission_journal import BatchSubmissionIntent


def expected_priorities(intent):
    if type(intent) is not BatchSubmissionIntent:raise TransferConflict()
    count=len(intent.inventory.files)
    probe=intent.candidate.probe_index
    if not 0<=probe<count:raise TransferConflict()
    return tuple(1 if i==probe else 0 for i in range(count))


def verify_probe_snapshot(intent,snapshot,*,require_stopped=False):
    if type(intent) is not BatchSubmissionIntent or type(snapshot) is not TransferSnapshot:
        raise TransferConflict()
    observed=snapshot.observation
    if (observed.info_hash!=intent.inventory.info_hash
            or observed.files!=intent.inventory.files
            or observed.save_path.rstrip('/')!=intent.options.download_root
            or observed.category!=intent.options.category
            or observed.auto_tmm is not False
            or 'ad-'+intent.marker not in observed.tags
            or any(t.startswith('ad-') and t!='ad-'+intent.marker for t in observed.tags)):
        raise TransferConflict()
    if require_stopped and observed.state not in STOPPED:
        raise TransferConflict()
    if observed.state not in STOPPED|CHECKING|ACTIVE:
        raise TransferConflict()
    if len(snapshot.piece_ranges)!=len(intent.inventory.files):
        raise TransferConflict()
    offset=0
    for file,pair in zip(intent.inventory.files,snapshot.piece_ranges):
        if file.size:
            if pair!=(offset//snapshot.piece_size,(offset+file.size-1)//snapshot.piece_size):
                raise TransferConflict()
        elif pair not in ((-1,-1),(offset//snapshot.piece_size,offset//snapshot.piece_size)):
            raise TransferConflict()
        offset+=file.size
    probe=intent.candidate.probe_index
    target=snapshot.piece_ranges[probe]
    if target[0]<0 or target[1]<target[0]:raise TransferConflict()
    return snapshot


def selection_fingerprint(intent,piece_size):
    if type(piece_size) is not int or not 1<=piece_size<=67108864:raise TransferConflict()
    return hashlib.sha256(json.dumps({
        'submission':intent.fingerprint,
        'info_sha256':intent.inventory.info_sha256,
        'probe':intent.candidate.probe_index,
        'priorities':expected_priorities(intent),
        'piece_size':piece_size,
        'overlap_bytes':0,
    },sort_keys=True,separators=(',',':')).encode()).hexdigest()


def exact_probe_selection(intent,snapshot):
    verify_probe_snapshot(intent,snapshot)
    return snapshot.priorities==expected_priorities(intent)


def classify_probe_progress(intent,snapshot):
    verify_probe_snapshot(intent,snapshot)
    if snapshot.observation.state in CHECKING:
        return 'START_UNCERTAIN','TORRENT_START_UNCERTAIN'
    if not exact_probe_selection(intent,snapshot):raise TransferConflict()
    index=intent.candidate.probe_index
    if snapshot.progress[index]==1:
        return 'COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
    if snapshot.observation.state in ACTIVE:
        return 'ACTIVE','TORRENT_DOWNLOAD_IN_PROGRESS'
    if snapshot.observation.state in STOPPED:
        return 'START_UNCERTAIN','TORRENT_START_UNCERTAIN'
    raise TransferConflict()
