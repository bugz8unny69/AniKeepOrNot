"""Immutable source inventory/intent publication under fenced worker completion."""
import sqlite3
import time
import uuid

from src.nextgen.acquisition.settings import SizeLimits, SizeEvidenceMissing
from src.nextgen.acquisition.contracts import AcquisitionInput, AcquisitionProposal
from src.nextgen.acquisition.metadata import ObservedInventory, AcquisitionConflict, MetadataInvalid, SourceMissing, infohash
from src.nextgen.episode_reservations import ReservationConflict
from src.nextgen.history import append_job_history, integer
from src.nextgen.job_contracts import parse_payload
from src.nextgen.storage.episode_reservations import _facts as episode_facts
from src.nextgen.storage.job_queue import QueueUnavailable
from src.nextgen.storage.migrations import LATEST_VERSION, MigrationError, _open


def _source(db,evaluation_job_id,event_id,*,cleanup=False):
    row=db.execute("SELECT s.reservation_id,s.owner_job_id,s.verification_id,s.plan_id,s.anidb_id,s.anidb_episode_id,"
        "s.tvdb_series_id,s.tvdb_season,s.tvdb_episode,s.tvdb_episode_id,r.anidb_episode_number,r.release_sha256,"
        "a.title,CASE WHEN length(e.raw_body)<=65536 THEN e.raw_body ELSE NULL END "
        "FROM episode_reservations s JOIN jobs j ON j.job_id=s.owner_job_id "
        "JOIN release_verifications r ON r.verification_id=s.verification_id "
        "JOIN identified_anime a ON a.anidb_id=s.anidb_id JOIN incoming_events e ON e.event_id=j.event_id "
        "WHERE s.evaluation_job_id=? AND j.event_id=? AND j.kind='RESERVE_EPISODE' AND j.state='DONE' AND s.state='ACTIVE'",
        (evaluation_job_id,event_id)).fetchone()
    if row is None:raise AcquisitionConflict()
    # Only exact-owned cancellation cleanup may read historical evidence for a
    # now-rejected anime. Submission always uses cleanup=False.
    if not cleanup:
        from src.nextgen.storage.review_decisions import require_allowed
        require_allowed(db,row[4])
    try:proof=episode_facts(db,evaluation_job_id,event_id)
    except ReservationConflict:raise AcquisitionConflict() from None
    if not proof.selected or row[2:10]!=(proof.verification_id,proof.plan_id,proof.anidb_id,proof.anidb_episode_id,
                                       proof.tvdb_series_id,proof.tvdb_season,proof.tvdb_episode,proof.tvdb_episode_id):
        raise AcquisitionConflict()
    try:
        document=parse_payload(row[13].decode('utf-8'))
        torrent_hash=infohash(document.get('torrent_infohash'))
    except (ValueError,TypeError,AttributeError,UnicodeError):raise SourceMissing() from None
    return AcquisitionInput(row[0],row[1],evaluation_job_id,event_id,torrent_hash,row[11],row[10],row[7],row[8],row[12])


def load_acquisition_input(database,evaluation_job_id,event_id):
    integer(evaluation_job_id,positive=True)
    integer(event_id,positive=True)
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0]!=LATEST_VERSION:raise QueueUnavailable()
        return _source(db,evaluation_job_id,event_id)
    except (OSError,sqlite3.Error,MigrationError):raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()


def source_magnet(database,source):
    """Hash-bound magnet for an accepted event: its sanitized declared magnet
    or, when none was declared, the bare info hash (Legacy behavior)."""
    from src.nextgen.acquisition.metadata import hash_magnet
    if type(source) is not AcquisitionInput:raise AcquisitionConflict()
    db=None
    try:
        db=_open(database,readonly=True,timeout_ms=5000)
        row=db.execute('SELECT raw_body FROM incoming_events WHERE event_id=?',(source.event_id,)).fetchone()
    except (OSError,sqlite3.Error,MigrationError):raise QueueUnavailable() from None
    finally:
        if db is not None:db.close()
    if row is None:raise SourceMissing()
    try:declared=parse_payload(bytes(row[0]).decode('utf-8')).get('magnet_uri')
    except (ValueError,TypeError,AttributeError,UnicodeError):raise SourceMissing() from None
    magnet=hash_magnet(source.info_hash,declared)
    if magnet is None:raise SourceMissing()
    return magnet


def validate_declaration(source,manifest):
    """The torrent has a selected video file.

    Title, season and episode were verified before reservation; the torrent is
    bound to that verified release by its info hash. Filenames are not re-checked.
    """
    try:manifest.files[manifest.selected_index]
    except (TypeError,IndexError,AttributeError):raise MetadataInvalid() from None


def _admit_size(db,value,job_id,now):
    source=value.source
    plan=db.execute('SELECT plan_id FROM episode_reservations WHERE reservation_id=?',(source.reservation_id,)).fetchone()[0]
    row=db.execute('SELECT max_episode_bytes,max_plan_bytes FROM acquisition_budgets WHERE user_evaluation_plan_id=?',(plan,)).fetchone()
    limits=value.limits if row is None else SizeLimits(row[0]//1000000,row[1]//1000000)
    prior=db.execute('SELECT p.job_id,p.selected_size_bytes FROM acquisition_plans p JOIN episode_reservations r '
                     "ON r.reservation_id=p.reservation_id WHERE r.plan_id=? AND r.state='ACTIVE' AND p.state='PREPARED'",(plan,)).fetchall()
    size=value.manifest.files[value.manifest.selected_index].size
    if any(n is None or (old_job==job_id and n!=size) for old_job,n in prior):raise SizeEvidenceMissing()
    limits.admit(size,sum(n for old_job,n in prior if old_job!=job_id))
    if row is None:
        db.execute('INSERT INTO acquisition_budgets VALUES(?,?,?,?,?)',
                   (plan,limits.episode_bytes,limits.plan_bytes,value.configuration_revision,now))
    return size


def persist_acquisition(db,claim,value):
    if not db.in_transaction or type(value) is not AcquisitionProposal:
        raise ValueError('worker transaction and typed acquisition facts required')
    source,manifest=value.source,value.manifest
    if claim.event_id!=source.event_id or _source(db,source.evaluation_job_id,claim.event_id)!=source:
        raise AcquisitionConflict()
    validate_declaration(source,manifest)
    admission=None
    if type(manifest) is ObservedInventory:
        from src.nextgen.storage.magnet_intake import _prior, _verified_candidate
        _verified_candidate(db,source)
        admission=_prior(db,source.event_id)
        inventory=db.execute('SELECT inventory_json FROM magnet_intents WHERE event_id=?',
                             (source.event_id,)).fetchone()
        if (admission is None or admission.job_id!=claim.job_id
                or admission.info_hash!=source.info_hash or admission.state!='OWNED_STOPPED'
                or inventory!=(manifest.json(),)):
            raise AcquisitionConflict()
    elif db.execute('SELECT 1 FROM magnet_intents WHERE event_id=?',
                    (source.event_id,)).fetchone():
        # Existing admission must be reconciled, never superseded by a new send.
        raise AcquisitionConflict()
    encoded=manifest.json()
    rows=db.execute("SELECT job_id,reservation_id,info_hash,info_sha256,manifest_json,configuration_revision "
                    "FROM acquisition_plans WHERE reservation_id=? OR (info_hash=? AND state='PREPARED')",
                    (source.reservation_id,source.info_hash)).fetchall()
    expected=(claim.job_id,source.reservation_id,manifest.info_hash,manifest.info_sha256,encoded,value.configuration_revision)
    now=integer(time.time_ns()//1000000)
    if rows:
        if len(rows)!=1 or rows[0]!=expected:raise AcquisitionConflict()
    size=_admit_size(db,value,claim.job_id,now)
    if not rows:
        db.execute("INSERT INTO acquisition_plans(job_id,reservation_id,info_hash,info_sha256,manifest_json,"
            "submission_marker,configuration_revision,prepared_at_ms,selected_size_bytes) VALUES(?,?,?,?,?,?,?,?,?)",
            (*expected[:5],admission.marker if admission else uuid.uuid4().hex,expected[5],now,size))
    if admission is not None:
        from src.nextgen.acquisition.submission import SubmissionPlan
        from src.nextgen.storage.submissions import _fingerprint
        plan_id=db.execute('SELECT plan_id FROM acquisition_plans WHERE job_id=?',
                           (claim.job_id,)).fetchone()[0]
        plan=SubmissionPlan(claim.job_id,plan_id,source.evaluation_job_id,
                            source.event_id,admission.marker,manifest)
        fingerprint=_fingerprint(plan,admission.options,admission.api_major)
        prior=db.execute('SELECT fingerprint,state FROM torrent_submissions WHERE plan_id=?',
                         (plan_id,)).fetchone()
        if prior is not None and prior!=(fingerprint,'OWNED_STOPPED'):raise AcquisitionConflict()
        if prior is None:
            db.execute("INSERT INTO torrent_submissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id,claim.job_id,claim.attempt,admission.options.base_url,
                 admission.options.download_root,admission.options.category,
                 admission.options.timeout_seconds,admission.api_major,fingerprint,
                 value.configuration_revision,'OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED',now,now))
    return append_job_history(db,job_id=claim.job_id,event_code='ACQUISITION_PREPARED',reason_code='ACQUISITION_PLAN_READY',
                              component='worker',occurred_at_ms=now,details={'attempt':claim.attempt})
