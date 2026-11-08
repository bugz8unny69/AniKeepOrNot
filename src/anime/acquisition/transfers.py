"""Transfer snapshots, exact selection and conservative boundary-piece policy."""
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading
from src.anime.acquisition.submission import Intent, Observation, ClientInvalid

from src.anime.acquisition.metadata_service import STOPPED  # The one definition of qBittorrent's stopped states.
CHECKING=frozenset(('checkingDL','checkingUP','checkingResumeData','allocating','moving'))
ACTIVE=frozenset(('downloading','stalledDL','forcedDL','queuedDL','uploading','stalledUP','forcedUP','queuedUP'))
MISSING_FILES='missingFiles'


class TransferConflict(ValueError):
    reason_code='TORRENT_TRANSFER_CONFLICT'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'Torrent selection, ownership or download details need review.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class PieceOverlap(ValueError):
    reason_code='TORRENT_PIECE_OVERLAP'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'This video shares torrent pieces with files that are not selected.')
        self.failed_check = failed_check
        self.failed_path = failed_path


class SnapshotChanged(ValueError):
    reason_code='TORRENT_SNAPSHOT_CHANGED'
    def __init__(self):super().__init__('The torrent changed while its status was being checked.')


@dataclass(frozen=True)
class TransferSnapshot:
    observation: Observation = field(repr=False)
    priorities: tuple[int,...]
    progress: tuple[float,...]
    piece_size: int
    piece_ranges: tuple[tuple[int,int],...] = field(repr=False)
    def __post_init__(self):
        if type(self.observation) is not Observation:raise ClientInvalid()
        count=len(self.observation.files)
        if (type(self.priorities) is not tuple or type(self.progress) is not tuple or type(self.piece_ranges) is not tuple
                or len(self.priorities)!=count or len(self.progress)!=count or len(self.piece_ranges)!=count
                or type(self.piece_size) is not int or not 1<=self.piece_size<=67108864):raise ClientInvalid()
        if any(type(p) is not int or p not in (0,1,6,7) for p in self.priorities):raise ClientInvalid()
        if any(type(p) not in (int,float) or not 0<=p<=1 or not math.isfinite(p) for p in self.progress):raise ClientInvalid()
        if any(type(r) is not tuple or len(r)!=2 or any(type(i) is not int or i< -1 for i in r) for r in self.piece_ranges):raise ClientInvalid()


def verify_snapshot(intent,snapshot):
    if type(intent) is not Intent or type(snapshot) is not TransferSnapshot:raise ClientInvalid()
    found=snapshot.observation
    if (found.info_hash!=intent.plan.manifest.info_hash or found.files!=intent.plan.manifest.files
            or found.save_path.rstrip('/')!=intent.options.download_root or found.category!=intent.options.category
            or found.auto_tmm is not False or 'ad-'+intent.plan.marker not in found.tags
            or any(t.startswith('ad-') and t!='ad-'+intent.plan.marker for t in found.tags)):raise TransferConflict()
    if found.state in ('error','missingFiles','unknown'):raise TransferConflict()
    offset=0
    for f,pair in zip(found.files,snapshot.piece_ranges):
        if f.size:
            if pair!=(offset//snapshot.piece_size,(offset+f.size-1)//snapshot.piece_size):raise TransferConflict()
        elif pair not in ((-1,-1),(offset//snapshot.piece_size,offset//snapshot.piece_size)):raise TransferConflict()
        offset+=f.size
    selected=intent.plan.manifest.selected_index
    start=sum(f.size for f in found.files[:selected])
    size=found.files[selected].size
    covered_start=start//snapshot.piece_size*snapshot.piece_size
    covered_end=min(offset,((start+size+snapshot.piece_size-1)//snapshot.piece_size)*snapshot.piece_size)
    return snapshot


def desired_priorities(intent):
    return tuple(1 if i==intent.plan.manifest.selected_index else 0 for i in range(len(intent.plan.manifest.files)))


def selection_fingerprint(intent,piece_size):
    return hashlib.sha256(json.dumps({'submission':intent.fingerprint,'priorities':desired_priorities(intent),
        'piece_size':piece_size,'overlap_bytes':0},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def exact_selection(intent,snapshot):return snapshot.priorities==desired_priorities(intent)


def transfer_state(intent,snapshot):
    verify_snapshot(intent,snapshot)
    state=snapshot.observation.state
    if state in CHECKING:return 'START_UNCERTAIN','TORRENT_START_UNCERTAIN'
    if not exact_selection(intent,snapshot):raise TransferConflict()
    if state not in STOPPED|ACTIVE:raise TransferConflict()
    if snapshot.progress[intent.plan.manifest.selected_index]==1:return 'COMPLETE','TORRENT_DOWNLOAD_COMPLETE'
    if state in ACTIVE:return 'ACTIVE','TORRENT_DOWNLOAD_IN_PROGRESS'
    return 'START_UNCERTAIN','TORRENT_START_UNCERTAIN'


class TransferPermit:
    """A process-local consumed authorization for fixed selection/start actions."""
    def __init__(self,fingerprint,actions):
        self.fingerprint=fingerprint
        self._remaining=set(actions)
        self._lock=threading.Lock()
    def take(self,fingerprint,action):
        with self._lock:
            if fingerprint!=self.fingerprint or action not in self._remaining:raise TransferConflict()
            self._remaining.remove(action)
