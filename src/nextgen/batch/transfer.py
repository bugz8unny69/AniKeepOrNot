"""Read-only batch qB selection preflight.

Returns intended priorities for the first probe; NEVER calls qBittorrent or
grants authority to perform an irreversible send. Actual selection/start must
have a separate durable fenced journal and single-use permit.
"""
from dataclasses import dataclass
import hashlib
import json

from src.nextgen.acquisition.metadata import BatchInventory
from src.nextgen.acquisition.settings import SizeLimits
from src.nextgen.acquisition.transfers import STOPPED, TransferSnapshot, TransferConflict
from src.nextgen.batch.verification import VerifiedBatch
from src.nextgen.batch.window import BatchWindowPlan


@dataclass(frozen=True)
class BatchProbeIntent:
    info_hash: str
    info_sha256: str
    selected_index: int
    priorities: tuple[int, ...]
    fingerprint: str
    expected_file_count: int
    selected_size_bytes: int
    # No credentials, paths or torrent source URLs in the printable receipt.


def plan_stopped_batch_probe(inventory, verified, window, snapshot, *,
                             ownership_marker, download_root, category, limits):
    """Validate exact torrent observation and produce only an E01-first intent.

    Missing ownership, changed file order/size, non-stopped clients and any
    torrent piece spanning a selected/unselected boundary fail closed. A
    qBittorrent selection is NOT applied here.
    """
    if (type(inventory) is not BatchInventory or type(verified) is not VerifiedBatch
            or type(window) is not BatchWindowPlan or type(snapshot) is not TransferSnapshot
            or type(limits) is not SizeLimits):
        raise ValueError('Typed batch proof, window, snapshot and limits required')
    if (type(ownership_marker) is not str or len(ownership_marker)!=32
            or any(c not in '0123456789abcdef' for c in ownership_marker)
            or type(download_root) is not str or not download_root
            or type(category) is not str or not category):
        raise ValueError('Trusted ownership and destination required')
    observed=snapshot.observation
    if (observed.info_hash!=inventory.info_hash
            or inventory.info_sha256!=verified.info_sha256
            or observed.files!=inventory.files
            or observed.state not in STOPPED
            or observed.save_path.rstrip('/')!=download_root.rstrip('/')
            or observed.category!=category
            or observed.auto_tmm is not False
            or 'ad-'+ownership_marker not in observed.tags
            or any(t.startswith('ad-') and t!='ad-'+ownership_marker for t in observed.tags)):
        raise TransferConflict()
    index=window.probe_index
    proof=next((f for f in verified.files if f.index==index),None)
    if proof is None or proof.tvdb_season!=verified.season or window.probe_episode!=proof.tvdb_episode:
        raise TransferConflict()
    if index not in {f.index for f in inventory.files} or index in window.skipped_indices:
        raise TransferConflict()
    file=inventory.files[index]
    limits.admit(file.size)
    if sum(inventory.files[i].size for _,i in window.present_episodes)>limits.plan_bytes:
        from src.nextgen.acquisition.settings import SizeLimitExceeded
        raise SizeLimitExceeded()
    if any(inventory.files[i].size>limits.episode_bytes for _,i in window.present_episodes):
        from src.nextgen.acquisition.settings import SizeLimitExceeded
        raise SizeLimitExceeded()
    if (len(snapshot.piece_ranges)!=len(inventory.files)
            or any(f.size>0 and snapshot.piece_ranges[i][0]<0 for i,f in enumerate(inventory.files))):
        raise TransferConflict()
    # Validate canonical BEP3 sequential piece boundaries, not reported
    # progress or client file names alone.
    offset=0
    for f,pair in zip(inventory.files,snapshot.piece_ranges):
        expected=(offset//snapshot.piece_size,(offset+f.size-1)//snapshot.piece_size) if f.size else (-1,-1)
        if f.size and pair!=expected:
            raise TransferConflict()
        offset+=f.size
    # Compare inclusive intervals directly: do not materialize a set of
    # potentially millions of pieces supplied by an untrusted client.
    wanted_start,wanted_end=snapshot.piece_ranges[index]
    if wanted_start<0 or wanted_end<wanted_start:
        raise TransferConflict()
    priorities=tuple(1 if i==index else 0 for i in range(len(inventory.files)))
    receipt=hashlib.sha256(json.dumps({
        'info_hash':inventory.info_hash,'info_sha256':inventory.info_sha256,
        'ownership_marker':ownership_marker,'file_index':index,'priorities':priorities,
        'piece_size':snapshot.piece_size},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return BatchProbeIntent(inventory.info_hash,inventory.info_sha256,index,priorities,
                            receipt,len(inventory.files),file.size)
