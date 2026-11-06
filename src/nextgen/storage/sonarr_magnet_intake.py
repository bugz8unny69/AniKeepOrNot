"""Pre-manifest legacy magnet admission using the existing fenced worker.

The marker is committed before a single network send. Replays can inspect
qBittorrent but cannot get another send permit. No magnet or passkey in SQLite.
This is not a replacement for the verified acquisition plan or download journal.
"""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import hashlib
import json
import re
import uuid

from src.nextgen.acquisition.contracts import AcquisitionInput
from src.nextgen.acquisition.metadata import AcquisitionConflict, infohash
from src.nextgen.acquisition.metadata_service import (
    is_metadata_expired, verify_torrent_ownership, METADATA_TIMEOUT_MS,
)
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import Observation, SendPermit
from src.nextgen.history import append_job_history
from src.nextgen.storage.acquisition import _source
from src.nextgen.storage.job_queue import JobQueue, Claim
from src.nextgen.storage.sqlite import transaction

from src.nextgen.acquisition.metadata_service import STOPPED  # The one definition of qBittorrent's stopped states.


@dataclass(frozen=True)
class MagnetIntent:
    event_id: int
    job_id: int
    info_hash: str
    marker: str = field(repr=False)
    options: DownloadOptions = field(repr=False)
    api_major: int
    state: str
    fingerprint: str

    def __post_init__(self):
        if (type(self.event_id) is not int or self.event_id<=0
                or type(self.job_id) is not int or self.job_id<=0
                or infohash(self.info_hash)!=self.info_hash
                or type(self.marker) is not str
                or not re.fullmatch('[a-f0-9]{32}',self.marker)
                or type(self.options) is not DownloadOptions
                or self.api_major not in (4,5)
                or self.state not in ('UNCERTAIN','OWNED_STOPPED','REVIEW','EXTERNAL','RELEASED')
                or type(self.fingerprint) is not str
                or not re.fullmatch('[a-f0-9]{64}',self.fingerprint)):
            raise AcquisitionConflict()


# Compatibility name for existing callers; all sends consume the shared permit.
MagnetPermit = SendPermit


def _fingerprint(event_id,hash_value,marker,options,api_major):
    body=json.dumps((event_id,hash_value,marker,options.base_url,
                     options.download_root,options.category,options.timeout_seconds,
                     api_major),separators=(',',':'))
    return hashlib.sha256(body.encode()).hexdigest()


def _verified_candidate(db,source):
    """The verified reservation that owns this magnet admission.

    Sonarr-discovery events must match their dispatched, active candidate.
    Ordinary webhook events (autobrr) are bound by their own ACTIVE exact
    episode reservation, which _source() has already re-verified.
    """
    if type(source) is not AcquisitionInput:
        raise AcquisitionConflict()
    origin=db.execute("SELECT source FROM incoming_events WHERE event_id=?",
                      (source.event_id,)).fetchone()
    if origin is None:raise AcquisitionConflict()
    if origin[0]!='sonarr-discovery':
        rows=db.execute(
            "SELECT plan_id FROM episode_reservations "
            "WHERE reservation_id=? AND evaluation_job_id=? AND state='ACTIVE' "
            "AND tvdb_season=? AND tvdb_episode=?",
            (source.reservation_id,source.evaluation_job_id,
             source.tvdb_season,source.tvdb_episode)).fetchall()
        if len(rows)!=1:raise AcquisitionConflict()
        # Intents are keyed like the trial plan: by AniDB episode number.
        return rows[0][0],source.anidb_episode_number
    rows=db.execute(
        "SELECT p.plan_id,c.anidb_episode_number FROM incoming_events e "
        "JOIN jobs verification ON verification.event_id=e.event_id "
        " AND verification.kind='VERIFY_RELEASE' AND verification.state='DONE' "
        "JOIN jobs parent ON parent.job_id=verification.created_by_job_id "
        " AND parent.kind='DISCOVER_SONARR_BACKFILL' "
        "JOIN trial_plans p ON p.plan_id=json_extract(parent.payload_json,'$.plan_id') "
        "JOIN sonarr_episode_selections selected ON selected.plan_id=p.plan_id "
        "JOIN sonarr_episode_candidates c ON c.plan_id=selected.plan_id "
        " AND c.anidb_episode_number=selected.anidb_episode_number AND c.info_hash=selected.info_hash "
        "WHERE e.event_id=? AND e.source='sonarr-discovery' "
        "AND selected.anidb_episode_number=? AND selected.tvdb_season=? "
        "AND selected.tvdb_episode=? AND selected.info_hash=? AND c.state='ACTIVE'",
        (source.event_id,source.anidb_episode_number,source.tvdb_season,source.tvdb_episode,
         source.info_hash)).fetchall()
    if len(rows)!=1:raise AcquisitionConflict()
    return rows[0]


def _restore(row):
    event_id,job_id,digest,marker,major,endpoint,root,category,timeout,state=row
    options=DownloadOptions(endpoint,root,category,timeout)
    return MagnetIntent(event_id,job_id,digest,marker,options,major,state,
                        _fingerprint(event_id,digest,marker,options,major))


def _prior(db,event_id):
    row=db.execute(
        "SELECT event_id,job_id,info_hash,marker,api_major,base_url,save_path,"
        "category,timeout_seconds,state FROM sonarr_magnet_intents WHERE event_id=?",
        (event_id,)).fetchone()
    return None if row is None else _restore(row)


def _matches_prior(prior,claim,source,options,api_major):
    return (prior.job_id==claim.job_id
            and prior.event_id==source.event_id
            and prior.info_hash==source.info_hash
            and prior.options.base_url==options.base_url
            and prior.options.category==options.category
            and prior.options.timeout_seconds==options.timeout_seconds
            and prior.options.download_root==str(
                PurePosixPath(options.download_root)/('ad-'+prior.marker)))


def release_cleaned(db,operation_id,now):
    """Retain history but retire hash claims only after drained cleanup/release."""
    if not db.in_transaction:raise ValueError('cleanup transaction required')
    if db.execute('SELECT state,cleanup_state FROM job_operations WHERE operation_id=?',
                  (operation_id,)).fetchone()!=('CANCELED','COMPLETE'):return
    from src.nextgen.storage.reservation_release import active
    if active(db,operation_id):return
    for table,state in (('acquisition_plans','PREPARED'),('staging_publications','PUBLISHED')):
        if db.execute('SELECT 1 FROM '+table+' p JOIN job_operation_members m ON m.job_id=p.job_id '
                      'WHERE m.operation_id=? AND p.state=? LIMIT 1',(operation_id,state)).fetchone():return
    db.execute("UPDATE sonarr_magnet_intents SET state='RELEASED',updated_at_ms=? "
               "WHERE job_id IN (SELECT job_id FROM job_operation_members WHERE operation_id=?) "
               "AND state<>'RELEASED'",(now,operation_id))


class MagnetJournal:
    def __init__(self,database,*,clock=None):
        self.queue=JobQueue(database,clock=clock)

    def existing(self,claim,source,options,api_major):
        """Read original admission for crash recovery without a Sonarr call.

        Ownership remains scoped to the same worker job, verified reservation,
        client endpoint, category and isolated download destination.
        """
        if (type(claim) is not Claim or claim.kind!='PREPARE_ACQUISITION'
                or type(source) is not AcquisitionInput
                or claim.event_id!=source.event_id
                or type(options) is not DownloadOptions
                or type(api_major) is not int or api_major not in (4,5)):
            raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            if _source(db,source.evaluation_job_id,source.event_id)!=source:
                raise AcquisitionConflict()
            prior=_prior(db,source.event_id)
            if prior is not None and not _matches_prior(
                    prior,claim,source,options,api_major):
                raise AcquisitionConflict()
            return prior

    def metadata_expired(self,claim,intent,*,timeout_ms=METADATA_TIMEOUT_MS):
        """Ten-minute acquisition limit; no repeat magnet send is permitted."""
        if type(timeout_ms) is not int or timeout_ms<60000:raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            self.queue._owns(db,claim,now)
            if _prior(db,intent.event_id)!=intent:raise AcquisitionConflict()
            row=db.execute('SELECT created_at_ms FROM sonarr_magnet_intents '
                           'WHERE event_id=?',(intent.event_id,)).fetchone()
            if row is None:raise AcquisitionConflict()
            return is_metadata_expired(row[0],now,timeout_ms=timeout_ms)

    def authorize(self,claim,source,options,api_major,present):
        """Commit irreversible uncertainty before issuing a send permit.

        A later attempt receives the same intent and NO permit even when the
        torrent is absent. Existing same-hash torrents always remain external.
        """
        if (type(claim) is not Claim or claim.kind!='PREPARE_ACQUISITION'
                or type(source) is not AcquisitionInput or source.event_id!=claim.event_id
                or type(options) is not DownloadOptions or type(present) is not bool
                or type(api_major) is not int or api_major not in (4,5)):
            raise AcquisitionConflict()
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            self.queue._owns(db,claim,now)
            if _source(db,source.evaluation_job_id,source.event_id)!=source:
                raise AcquisitionConflict()
            plan_id,episode=_verified_candidate(db,source)
            prior=_prior(db,source.event_id)
            if prior is not None:
                if not _matches_prior(prior,claim,source,options,api_major):
                    raise AcquisitionConflict()
                return prior,None,()
            if db.execute(
                "SELECT 1 FROM acquisition_plans WHERE reservation_id=?",
                (source.reservation_id,)).fetchone():
                raise AcquisitionConflict()
            # Older completed cleanups predate RELEASED magnet intents. Retire
            # those claims here as well, without erasing the original journal.
            conflict=db.execute("SELECT job_id FROM sonarr_magnet_intents "
                                "WHERE info_hash=? AND state<>'RELEASED'",(source.info_hash,)).fetchone()
            if conflict is not None:
                owner=db.execute('SELECT operation_id FROM job_operation_members WHERE job_id=?',conflict).fetchone()
                if owner is not None:release_cleaned(db,owner[0],now)
                if db.execute("SELECT 1 FROM sonarr_magnet_intents WHERE info_hash=? AND state<>'RELEASED'",
                              (source.info_hash,)).fetchone():raise AcquisitionConflict()
            marker=uuid.uuid4().hex
            target=DownloadOptions(options.base_url,
                str(PurePosixPath(options.download_root)/('ad-'+marker)),
                options.category,options.timeout_seconds)
            state='EXTERNAL' if present else 'UNCERTAIN'
            db.execute(
                "INSERT INTO sonarr_magnet_intents"
                "(event_id,job_id,plan_id,anidb_episode_number,info_hash,marker,api_major,"
                "base_url,save_path,category,state,created_at_ms,updated_at_ms,"
                "timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source.event_id,claim.job_id,plan_id,episode,source.info_hash,
                 marker,api_major,target.base_url,target.download_root,
                 target.category,state,now,now,target.timeout_seconds))
            intent=_prior(db,source.event_id)
            receipt=append_job_history(
                db,job_id=claim.job_id,event_code='TORRENT_INTENT_RECORDED',
                reason_code=('TORRENT_ALREADY_PRESENT' if present else
                             'TORRENT_SUBMISSION_UNCERTAIN'),
                component='worker',occurred_at_ms=now,
                details={'attempt':claim.attempt})
            return intent,(None if present else MagnetPermit(intent.fingerprint)),(receipt,)

    def guard(self,claim,intent):
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            current=_prior(db,intent.event_id)
            if (current!=intent or current.job_id!=claim.job_id
                    or current.state!='UNCERTAIN'):
                raise AcquisitionConflict()

    def capture(self,claim,intent,source,inventory):
        """Persist only explicitly tagged, stopped metadata observations.

        This file inventory has no BEP3 info SHA. Later phases must opt in to
        its separate evidence contract when handing off the original admission.
        """
        from src.nextgen.acquisition.metadata import ObservedInventory
        from src.nextgen.storage.acquisition import validate_declaration
        if (type(inventory) is not ObservedInventory
                or type(source) is not AcquisitionInput
                or inventory.info_hash!=source.info_hash
                or source.event_id!=intent.event_id):
            raise AcquisitionConflict()
        validate_declaration(source,inventory)
        encoded=inventory.json()
        with self.queue.connection() as db,transaction(db):
            self.queue._owns(db,claim,self.queue.now())
            if (_source(db,source.evaluation_job_id,source.event_id)!=source
                    or _prior(db,intent.event_id).state!='OWNED_STOPPED'):
                raise AcquisitionConflict()
            row=db.execute(
                "SELECT inventory_json FROM sonarr_magnet_intents "
                "WHERE event_id=? AND job_id=? AND info_hash=? AND marker=?",
                (intent.event_id,intent.job_id,intent.info_hash,intent.marker)
            ).fetchone()
            if row is None or row[0] is not None and row[0]!=encoded:
                raise AcquisitionConflict()
            if row[0] is None:
                updated=db.execute(
                    "UPDATE sonarr_magnet_intents SET inventory_json=?,"
                    "updated_at_ms=? WHERE event_id=? AND inventory_json IS NULL",
                    (encoded,self.queue.now(),intent.event_id))
                if updated.rowcount!=1:raise AcquisitionConflict()
            return inventory

    def reconcile(self,claim,intent,observation):
        """Never adopt another client's torrent or grant a second send."""
        with self.queue.connection() as db,transaction(db):
            now=self.queue.now()
            self.queue._owns(db,claim,now)
            current=_prior(db,intent.event_id)
            if current!=intent or current.job_id!=claim.job_id:
                raise AcquisitionConflict()
            if current.state in ('REVIEW','EXTERNAL','OWNED_STOPPED'):
                return current.state
            state='UNCERTAIN'
            if observation is not None:
                if type(observation) is not Observation:raise AcquisitionConflict()
                if (observation.info_hash!=intent.info_hash
                        or not verify_torrent_ownership(observation,intent.marker,
                                                        intent.options.download_root,
                                                        intent.options.category)):
                    state='REVIEW'
                elif observation.files and observation.state in STOPPED:
                    state='OWNED_STOPPED'
            if state!=current.state:
                db.execute(
                    "UPDATE sonarr_magnet_intents SET state=?,updated_at_ms=? "
                    "WHERE event_id=? AND state='UNCERTAIN'",
                    (state,now,intent.event_id))
            return state
