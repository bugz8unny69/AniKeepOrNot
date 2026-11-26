#!/usr/bin/env python3
"""One-time cutover from old per-step jobs to the series table (CO-1 to CO-5).

Preview by default. With --apply:
1. Refuses unless API and worker services are stopped.
2. Backs up the SQLite database alongside the original (0600).
3. Applies migration 079 (079_series.sql) if the database is at v78.
4. Converts staged episodes, unfinished work, searches, and decisions into
   `series` rows (one row per AniDB ID) with fresh history, and marks
   converted unfinished jobs CANCELED ('CUTOVER_CONVERTED') so migration 080
   can run.
5. Prints the report (per series, torrents per step, closed jobs, and any
   jobs that could not be converted with their ID and reason).
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.anime.configuration import ConfigurationStore  # noqa: E402
from src.anime.storage import migrations  # noqa: E402

UNITS_TO_CHECK = (
    "anime-discovery-api.service",
    "anime-discovery-worker.service",
    "akon-api.service",
    "akon-worker.service",
)

# Maps old job kind -> target torrent step (or special handler category).
STEP_BY_KIND = {
    "VERIFY_RELEASE": "get_metadata",
    "EVALUATE_BATCH": "get_metadata",
    "RESERVE_EPISODE": "get_metadata",
    "RESERVE_BATCH": "get_metadata",
    "PREPARE_ACQUISITION": "get_metadata",
    "SELECT_USER_EVALUATION": "get_metadata",
    "SUBMIT_TORRENT": "download",
    "DOWNLOAD_TORRENT": "download",
    "SUBMIT_BATCH": "download",
    "TRANSFER_BATCH": "download",
    "UNLOCK_BATCH": "download",
    "VERIFY_MEDIA": "check_media",
    "VERIFY_BATCH_PROBE": "check_media",
    "VERIFY_BATCH_EPISODE": "check_media",
    "TRANSCODE_MEDIA": "check_media",
    "STAGE_USER_EVALUATION_MEDIA": "check_media",
    "STAGE_BATCH_EPISODE": "check_media",
}

SEARCH_KINDS = frozenset({
    "DISCOVER_EPISODE_RELEASES",
    "DISCOVER_BATCH_PROWLARR",
    "RESET_EPISODE_SEARCH",
})

KEEP_KINDS = frozenset({
    "KEEP_MEDIA",
    "KEEP_BATCH",
})

HOUSEKEEPING_KINDS = frozenset({
    "CLEANUP_DECIDED_DOWNLOAD",
    "CLEANUP_KEPT_MEDIA",
    "CLEANUP_CANCELED_OPERATION",
    "CLEANUP_BATCH_DOWNLOAD",
    "CLEANUP_BATCH_KEPT_MEDIA",
    "REFRESH_JELLYFIN",
    "LINK_SHOKO",
    "FIND_DUBS_FOR_SONARR",
    "ENRICH_SHOKO_METADATA",
    "BULK_MANAGE_JOBS",
})

STOPPED_JOB_STATES = frozenset({"NEEDS_REVIEW", "ON_HOLD", "BLOCKED"})


def services_stopped() -> list[str]:
    """Return list of active service unit names; empty means all stopped."""
    if shutil.which("systemctl") is None:
        return []
    active = []
    for unit in UNITS_TO_CHECK:
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
        )
        if proc.returncode == 0:
            active.append(unit)
    return active


def configured_database(config_dir: Path) -> Path:
    return Path(ConfigurationStore(config_dir).read().value("storage.database_path"))


def open_database(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Database not found: {path}")
    uri = f"file:{path}?mode=ro" if readonly else str(path)
    db = sqlite3.connect(uri, uri=readonly, timeout=60.0)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def backup(db: sqlite3.Connection, path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.before-cutover-{stamp}.sqlite")
    with closing(sqlite3.connect(dest)) as target:
        db.backup(target)
    os.chmod(dest, 0o600)
    return dest


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _ep_code(season: int, episode: int) -> str:
    return f"S{int(season):02d}E{int(episode):02d}"


def _series_identity(db: sqlite3.Connection, anidb_id: int) -> tuple[str, int | None, dict | None]:
    """Resolve display title, tvdb_series_id, and series_json identity dict."""
    row = db.execute(
        "SELECT ia.tvdb_series_id, ia.title, "
        "COALESCE(atm.english_title, atm.romaji_title, rs.display_title, ia.title) "
        "FROM identified_anime ia "
        "LEFT JOIN anime_title_metadata atm ON atm.anidb_id = ia.anidb_id "
        "LEFT JOIN recognized_series rs ON rs.anidb_id = ia.anidb_id "
        "WHERE ia.anidb_id = ?",
        (anidb_id,),
    ).fetchone()
    if row:
        tvdb_series_id, base_title, display_title = int(row[0]), row[1], row[2]
        identity = {
            "anime": {
                "anidb_id": anidb_id,
                "tvdb_series_id": tvdb_series_id,
                "title": base_title,
            }
        }
        return display_title, tvdb_series_id, identity

    rs_row = db.execute(
        "SELECT display_title, tvdb_series_id FROM recognized_series WHERE anidb_id = ?",
        (anidb_id,),
    ).fetchone()
    if rs_row:
        tvdb_series_id = int(rs_row[1]) if rs_row[1] is not None else None
        identity = (
            {"anime": {"anidb_id": anidb_id, "tvdb_series_id": tvdb_series_id, "title": rs_row[0]}}
            if tvdb_series_id is not None
            else None
        )
        return rs_row[0], tvdb_series_id, identity

    return f"AniDB {anidb_id}", None, None


def _identity_from_event(db: sqlite3.Connection, event_id: int | None, anidb_id: int) -> dict | None:
    if event_id is not None:
        row = db.execute(
            "SELECT series_json FROM identity_results WHERE event_id=? AND anidb_id=?",
            (event_id, anidb_id),
        ).fetchone()
        if row:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict) and isinstance(parsed.get("anime"), dict):
                return parsed
    _, _, fallback = _series_identity(db, anidb_id)
    return fallback


def _raw_event_info(db: sqlite3.Connection, event_id: int | None) -> tuple[str | None, str | None, str | None]:
    """Return (raw_body_json_str, release_name, infohash) from incoming_events."""
    if event_id is None:
        return None, None, None
    row = db.execute("SELECT raw_body FROM incoming_events WHERE event_id=?", (event_id,)).fetchone()
    if not row or row[0] is None:
        return None, None, None
    raw_str = row[0].decode("utf-8", errors="replace") if isinstance(row[0], bytes) else str(row[0])
    try:
        ev = json.loads(raw_str)
    except json.JSONDecodeError:
        return raw_str, None, None
    if not isinstance(ev, dict):
        return raw_str, None, None
    ih = ev.get("torrent_infohash") or ev.get("info_hash")
    if isinstance(ih, str):
        ih = ih.strip().lower()
    rn = ev.get("release_name")
    norm = {}
    if rn:
        norm["release_name"] = rn
    if ih:
        norm["torrent_infohash"] = ih
    if ev.get("magnet_uri"):
        norm["magnet_uri"] = ev["magnet_uri"]
    return (json.dumps(norm, separators=(",", ":")) if norm else raw_str), rn, ih


def _qb_state_from_single(db: sqlite3.Connection, plan_id: int, marker: str | None) -> dict | None:
    if not marker:
        return None
    sub = db.execute("SELECT state FROM torrent_submissions WHERE plan_id=?", (plan_id,)).fetchone()
    if sub is None:
        return None
    qb = {"marker": marker, "added": "sent"}
    tr = db.execute("SELECT phase FROM torrent_transfers WHERE plan_id=?", (plan_id,)).fetchone()
    if tr:
        phase = tr[0]
        if phase in ("SELECTED", "START_UNCERTAIN", "ACTIVE", "COMPLETE"):
            qb["selected"] = True
        if phase in ("START_UNCERTAIN", "ACTIVE", "COMPLETE"):
            qb["started"] = True
    return qb


def _qb_state_from_batch(db: sqlite3.Connection, plan_id: int) -> dict | None:
    sub = db.execute(
        "SELECT ownership_marker, state FROM batch_torrent_submissions WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if sub is None:
        return None
    qb = {"marker": sub[0], "added": "sent"}
    tr = db.execute("SELECT phase FROM batch_torrent_transfers WHERE plan_id=?", (plan_id,)).fetchone()
    if tr:
        phase = tr[0]
        if phase in ("SELECTED", "START_UNCERTAIN", "ACTIVE", "COMPLETE"):
            qb["selected"] = True
        if phase in ("START_UNCERTAIN", "ACTIVE", "COMPLETE"):
            qb["started"] = True
    return qb


def _torrent_name_from_manifest(manifest_json: str) -> str | None:
    manifest = json.loads(manifest_json)
    return manifest.get("name") or manifest.get("torrent_name")


def _single_file_entry(
    manifest_json: str,
    code: str,
    anidb_ep_num: int,
    anidb_ep_id: int,
    tvdb_ep_id: int,
    evidence_json: str | None = None,
    staged_file: list | None = None,
) -> dict:
    manifest = json.loads(manifest_json)
    selected = manifest.get("selected") or {}
    if not selected and manifest.get("files"):
        selected = manifest["files"][0]
    entry = {
        "index": selected.get("index", 0),
        "path": selected.get("path", ""),
        "size": selected.get("size", 0),
        "episode": code,
        "anidb_episode_number": anidb_ep_num,
        "anidb_episode_id": anidb_ep_id,
        "tvdb_episode_id": tvdb_ep_id,
    }
    if evidence_json:
        ev = json.loads(evidence_json)
        entry["media"] = ev
        if ev.get("path"):
            entry["local_path"] = ev["path"]
    if staged_file is not None:
        entry["staged_file"] = staged_file
    return entry


def _resolve_job_anidb_and_infohash(db: sqlite3.Connection, job_id: int, event_id: int | None, payload: dict) -> tuple[int | None, str | None, str | None, str | None]:
    """Return (anidb_id, infohash, release_name, raw_body)."""
    raw_body, ev_rn, ev_ih = _raw_event_info(db, event_id)
    anidb_id = payload.get("anidb_id")
    if anidb_id is not None:
        anidb_id = int(anidb_id)

    infohash = payload.get("info_hash") or payload.get("torrent_infohash") or ev_ih
    if isinstance(infohash, str):
        infohash = infohash.strip().lower()
    release_name = payload.get("release_name") or ev_rn

    if anidb_id is None and event_id is not None:
        ir = db.execute("SELECT anidb_id FROM identity_results WHERE event_id=?", (event_id,)).fetchone()
        if ir:
            anidb_id = int(ir[0])

    # Walk job-kind specific links if still missing
    if payload.get("evaluation_job_id") is not None:
        ev_job_id = int(payload["evaluation_job_id"])
        row = db.execute(
            "SELECT er.anidb_id, ap.info_hash, ap.manifest_json "
            "FROM episode_reservations er "
            "LEFT JOIN acquisition_plans ap ON ap.reservation_id = er.reservation_id "
            "WHERE er.evaluation_job_id = ?",
            (ev_job_id,),
        ).fetchone()
        if row:
            anidb_id = anidb_id or int(row[0])
            if row[1] and not infohash:
                infohash = row[1]
            if row[2] and not release_name:
                release_name = _torrent_name_from_manifest(row[2])

    if payload.get("verification_job_id") is not None and anidb_id is None:
        vj_id = int(payload["verification_job_id"])
        rv = db.execute("SELECT anidb_id FROM release_verifications WHERE job_id=?", (vj_id,)).fetchone()
        if rv:
            anidb_id = int(rv[0])

    if payload.get("plan_id") is not None:
        pid = int(payload["plan_id"])
        ap = db.execute(
            "SELECT ap.info_hash, ap.manifest_json, er.anidb_id "
            "FROM acquisition_plans ap "
            "JOIN episode_reservations er ON er.reservation_id = ap.reservation_id "
            "WHERE ap.plan_id = ?",
            (pid,),
        ).fetchone()
        if ap:
            infohash = infohash or ap[0]
            release_name = release_name or _torrent_name_from_manifest(ap[1])
            anidb_id = anidb_id or int(ap[2])

    # Check if job_id owns an acquisition_plan, reservation, or batch
    row = db.execute(
        "SELECT er.anidb_id, ap.info_hash, ap.manifest_json "
        "FROM episode_reservations er "
        "LEFT JOIN acquisition_plans ap ON ap.reservation_id = er.reservation_id "
        "WHERE er.owner_job_id = ? OR ap.job_id = ?",
        (job_id, job_id),
    ).fetchone()
    if row:
        anidb_id = anidb_id or int(row[0])
        infohash = infohash or row[1]
        if row[2] and not release_name:
            release_name = _torrent_name_from_manifest(row[2])

    bv = db.execute(
        "SELECT bv.anidb_id, bv.info_hash "
        "FROM batch_verifications bv "
        "LEFT JOIN batch_user_evaluation_plans buep ON buep.batch_id = bv.batch_id "
        "LEFT JOIN batch_torrent_submissions bts ON bts.plan_id = buep.plan_id "
        "LEFT JOIN batch_torrent_transfers btt ON btt.plan_id = buep.plan_id "
        "WHERE bv.job_id = ? OR buep.evaluation_job_id = ? OR bts.reservation_job_id = ? "
        "OR bts.first_job_id = ? OR btt.transfer_job_id = ?",
        (job_id, job_id, job_id, job_id, job_id),
    ).fetchone()
    if bv:
        anidb_id = anidb_id or int(bv[0])
        infohash = infohash or bv[1]

    if anidb_id is None:
        rv = db.execute("SELECT anidb_id FROM release_verifications WHERE job_id=?", (job_id,)).fetchone()
        if rv:
            anidb_id = int(rv[0])

    if anidb_id is None and payload.get("operation_id") is not None:
        op_id = int(payload["operation_id"])
        rv = db.execute(
            "SELECT er.anidb_id FROM job_operation_members m "
            "JOIN episode_reservations er ON er.owner_job_id = m.job_id "
            "WHERE m.operation_id = ?",
            (op_id,),
        ).fetchone()
        if rv:
            anidb_id = int(rv[0])

    return anidb_id, infohash, release_name, raw_body


def _build_keep_work(db: sqlite3.Connection, job_id: int, created_at_ms: int) -> tuple[int | None, dict | None, str | None]:
    """Convert an active KEEP_MEDIA or KEEP_BATCH job into series.work['keep']."""
    # Single episode keep
    ki = db.execute("SELECT phase, data_json, updated_at_ms FROM keep_imports WHERE job_id=?", (job_id,)).fetchone()
    if ki:
        phase, data_json, _ = ki
        data = json.loads(data_json)
        sel = data.get("selection") or {}
        aid = sel.get("anidb_id")
        if aid is None:
            return None, None, "keep_imports selection missing anidb_id"
        code = _ep_code(sel["tvdb_season"], sel["tvdb_episode"])
        step = "sonarr_series" if phase in ("PLANNED", "ADD_UNCERTAIN") else "sonarr_import"
        sonarr_state = {"phase": "PLANNED"}
        if phase == "ADD_UNCERTAIN":
            sonarr_state = {"phase": "ADD_UNCERTAIN", "monitor": data.get("monitor", "all")}
        elif phase in ("SERIES_READY", "IMPORT_UNCERTAIN", "COMPLETE"):
            series_rec = data.get("series") or {}
            sonarr_state = {
                "phase": "READY",
                "series_id": series_rec.get("id"),
                "series_path": series_rec.get("path"),
                "tvdb_series_id": sel.get("tvdb_series_id"),
            }
        ep_import = {"phase": phase}
        for key in ("episode_id", "command_id", "payload", "source_file", "result"):
            if key in data:
                ep_import[key] = data[key]
        return int(aid), {
            "requested_at_ms": created_at_ms,
            "step": step,
            "sonarr": sonarr_state,
            "imports": {code: ep_import},
        }, None

    # Batch keep
    bki_rows = db.execute(
        "SELECT file_index, phase, data_json FROM batch_keep_imports WHERE job_id=? ORDER BY file_index",
        (job_id,),
    ).fetchall()
    if bki_rows:
        aid = None
        tvdb_id = None
        sonarr_state = {"phase": "PLANNED"}
        imports = {}
        overall_step = "sonarr_import"
        for _, phase, data_json in bki_rows:
            data = json.loads(data_json)
            sel = data.get("selection") or {}
            if aid is None and sel.get("anidb_id") is not None:
                aid = int(sel["anidb_id"])
                tvdb_id = sel.get("tvdb_series_id")
            if sel.get("tvdb_season") is not None and sel.get("tvdb_episode") is not None:
                code = _ep_code(sel["tvdb_season"], sel["tvdb_episode"])
                ep_import = {"phase": phase}
                for key in ("episode_id", "command_id", "payload", "source_file", "result"):
                    if key in data:
                        ep_import[key] = data[key]
                imports[code] = ep_import
            if phase in ("PLANNED", "ADD_UNCERTAIN"):
                overall_step = "sonarr_series"
                if phase == "ADD_UNCERTAIN":
                    sonarr_state = {"phase": "ADD_UNCERTAIN", "monitor": data.get("monitor", "all")}
            elif phase in ("SERIES_READY", "IMPORT_UNCERTAIN", "COMPLETE") and sonarr_state["phase"] != "READY":
                series_rec = data.get("series") or {}
                sonarr_state = {
                    "phase": "READY",
                    "series_id": series_rec.get("id"),
                    "series_path": series_rec.get("path"),
                    "tvdb_series_id": tvdb_id,
                }
        if aid is None:
            return None, None, "batch_keep_imports missing anidb_id"
        return aid, {
            "requested_at_ms": created_at_ms,
            "step": overall_step,
            "sonarr": sonarr_state,
            "imports": imports,
        }, None

    # KEEP_* job with no keep_imports rows yet (never started / failed before planning)
    row = db.execute("SELECT payload_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    payload = json.loads(row[0]) if row else {}
    aid, _, _, _ = _resolve_job_anidb_and_infohash(db, job_id, None, payload)
    if aid is None:
        return None, None, "KEEP job has no keep_imports row and no resolvable anidb_id"
    return aid, {"requested_at_ms": created_at_ms, "step": "cancel", "imports": {}}, None


def inspect_cutover(db: sqlite3.Connection, now_ms: int | None = None) -> dict:
    """Inspect v78/v79 database and build the exact conversion plan."""
    if not _table_exists(db, "jobs"):
        return {
            "already_cut_over": True,
            "series_to_convert": [],
            "converted_job_ids": [],
            "job_outcomes": [],
            "refused_jobs": [],
            "step_counts": {},
        }

    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000

    series_map: dict[int, dict] = {}

    def ensure_series(aid: int) -> dict:
        if aid not in series_map:
            title, tvdb_id, identity = _series_identity(db, aid)
            series_map[aid] = {
                "anidb_id": aid,
                "title": title,
                "tvdb_series_id": tvdb_id,
                "identity": identity,
                "state": "ASLEEP",
                "wake_at_ms": None,
                "torrents": {},
                "episodes": {},
                "work": {},
            }
        return series_map[aid]

    def ensure_torrent(entry: dict, infohash: str, release_name: str | None, raw_body: str | None,
                       identity: dict | None, step: str, state: str, why: str | None = None,
                       qb: dict | None = None) -> dict:
        if infohash not in entry["torrents"]:
            entry["torrents"][infohash] = {
                "infohash": infohash,
                "release_name": release_name or infohash,
                "raw_body": raw_body,
                "step": step,
                "state": state,
                "why": why,
                "episodes": [],
                "files": [],
                "progress": 1.0 if step == "done" else 0.0,
                "submitted_at_ms": None,
                "identity": identity or entry["identity"],
            }
            if qb is not None:
                entry["torrents"][infohash]["qb"] = qb
        return entry["torrents"][infohash]

    # 1. Decisions (CO-4)
    for aid, decision, _ in db.execute(
        "SELECT anidb_id, decision, cleanup_state FROM anime_review_decisions"
    ).fetchall():
        entry = ensure_series(int(aid))
        entry["state"] = "KEPT" if decision == "ACCEPT" else "REJECTED"

    # 2. Staged single-episode publications (CO-2)
    single_staged_rows = db.execute(
        "SELECT er.anidb_id, er.anidb_episode_id, rv.anidb_episode_number, er.tvdb_series_id, "
        "er.tvdb_season, er.tvdb_episode, er.tvdb_episode_id, "
        "ap.plan_id, ap.info_hash, ap.submission_marker, ap.manifest_json, j.event_id, "
        "mv.evidence_json, sp.manifest_json, sp.receipts_json "
        "FROM staging_publications sp "
        "JOIN media_verifications mv ON mv.plan_id = sp.plan_id "
        "JOIN acquisition_plans ap ON ap.plan_id = sp.plan_id "
        "JOIN jobs j ON j.job_id = ap.job_id "
        "JOIN episode_reservations er ON er.reservation_id = ap.reservation_id "
        "JOIN release_verifications rv ON rv.verification_id = er.verification_id "
        "WHERE sp.state = 'PUBLISHED' "
        "ORDER BY sp.plan_id"
    ).fetchall()

    for (aid, anidb_ep_id, anidb_ep_num, tvdb_series_id, tvdb_season, tvdb_ep, tvdb_ep_id,
         plan_id, info_hash, marker, ap_manifest, event_id, evidence_json, sp_manifest, sp_receipts) in single_staged_rows:
        aid = int(aid)
        entry = ensure_series(aid)
        code = _ep_code(tvdb_season, tvdb_ep)
        plan = json.loads(sp_manifest)
        receipts = json.loads(sp_receipts)
        media_path = next(e["path"] for e in plan["entries"] if e["role"] == "media")
        media_receipt = next(r["file"] for r in receipts if r["role"] == "media")
        raw_body, ev_rn, _ = _raw_event_info(db, event_id)
        identity = _identity_from_event(db, event_id, aid)
        qb = _qb_state_from_single(db, plan_id, marker) or {
            "marker": marker, "added": "sent", "selected": True, "started": True
        }
        t = ensure_torrent(
            entry, info_hash, ev_rn or _torrent_name_from_manifest(ap_manifest), raw_body,
            identity, "done", "done", qb=qb,
        )
        if code not in t["episodes"]:
            t["episodes"].append(code)
        if not any(f.get("episode") == code for f in t["files"]):
            t["files"].append(_single_file_entry(
                ap_manifest, code, int(anidb_ep_num), int(anidb_ep_id), int(tvdb_ep_id),
                evidence_json=evidence_json, staged_file=media_receipt,
            ))
        t.setdefault("staging", {"plans": {}, "artifacts": []})["plans"][code] = plan
        ep_state = "kept" if entry["state"] == "KEPT" else ("dropped" if entry["state"] == "REJECTED" else "staged")
        entry["episodes"][code] = {"state": ep_state, "torrent": info_hash, "file": media_path}

    # 3. Staged season-pack publications (CO-2)
    batch_staged_rows = db.execute(
        "SELECT bv.anidb_id, bv.tvdb_series_id, bv.info_hash, bv.job_id, "
        "bvf.file_index, bvf.file_path, bvf.file_size_bytes, bvf.anidb_episode_number, "
        "bvf.anidb_episode_id, bvf.tvdb_season, bvf.tvdb_episode, bvf.tvdb_episode_id, "
        "bsp.plan_id, bem.evidence_json, bsp.manifest_json, bsp.receipts_json, j.event_id "
        "FROM batch_staging_publications bsp "
        "JOIN batch_episode_media bem ON bem.plan_id = bsp.plan_id AND bem.file_index = bsp.file_index "
        "JOIN batch_user_evaluation_files buef ON buef.plan_id = bsp.plan_id AND buef.file_index = bsp.file_index "
        "JOIN batch_user_evaluation_plans buep ON buep.plan_id = bsp.plan_id "
        "JOIN batch_verifications bv ON bv.batch_id = buep.batch_id "
        "JOIN batch_verified_files bvf ON bvf.batch_id = bv.batch_id AND bvf.file_index = bsp.file_index "
        "JOIN jobs j ON j.job_id = bv.job_id "
        "WHERE bsp.state = 'PUBLISHED' "
        "ORDER BY bsp.plan_id, bsp.file_index"
    ).fetchall()

    for (aid, tvdb_series_id, info_hash, bv_job_id, file_index, file_path, file_size_bytes,
         anidb_ep_num, anidb_ep_id, tvdb_season, tvdb_ep, tvdb_ep_id,
         plan_id, evidence_json, bsp_manifest, bsp_receipts, event_id) in batch_staged_rows:
        aid = int(aid)
        entry = ensure_series(aid)
        code = _ep_code(tvdb_season, tvdb_ep)
        plan = json.loads(bsp_manifest)
        receipts = json.loads(bsp_receipts)
        media_path = next(e["path"] for e in plan["entries"] if e["role"] == "media")
        media_receipt = next(r["file"] for r in receipts if r["role"] == "media")
        raw_body, ev_rn, _ = _raw_event_info(db, event_id)
        identity = _identity_from_event(db, event_id, aid)
        qb = _qb_state_from_batch(db, plan_id)
        t = ensure_torrent(
            entry, info_hash, ev_rn or info_hash, raw_body,
            identity, "done", "done", qb=qb,
        )
        if code not in t["episodes"]:
            t["episodes"].append(code)
        if not any(f.get("episode") == code for f in t["files"]):
            ev = json.loads(evidence_json)
            fentry = {
                "index": int(file_index),
                "path": file_path,
                "size": int(file_size_bytes),
                "episode": code,
                "anidb_episode_number": int(anidb_ep_num),
                "anidb_episode_id": int(anidb_ep_id),
                "tvdb_episode_id": int(tvdb_ep_id),
                "media": ev,
                "staged_file": media_receipt,
            }
            if ev.get("path"):
                fentry["local_path"] = ev["path"]
            t["files"].append(fentry)
        t.setdefault("staging", {"plans": {}, "artifacts": []})["plans"][code] = plan
        ep_state = "kept" if entry["state"] == "KEPT" else ("dropped" if entry["state"] == "REJECTED" else "staged")
        entry["episodes"][code] = {"state": ep_state, "torrent": info_hash, "file": media_path}

    # 4. Unfinished jobs (CO-1, CO-3, CO-4)
    unfinished = db.execute(
        "SELECT job_id, event_id, kind, state, payload_json, last_reason_code, created_at_ms, updated_at_ms "
        "FROM jobs WHERE state NOT IN ('DONE', 'CANCELED', 'FAILED') ORDER BY job_id"
    ).fetchall()

    converted_job_ids: list[int] = []
    job_outcomes: list[dict] = []
    refused_jobs: list[dict] = []
    step_counts: dict[str, int] = {}

    for job_id, event_id, kind, job_state, payload_json, reason_code, created_ms, updated_ms in unfinished:
        payload = json.loads(payload_json)
        is_stopped = job_state in STOPPED_JOB_STATES
        t_state = "stopped" if is_stopped else "waiting"
        why = reason_code if is_stopped else None

        if kind in HOUSEKEEPING_KINDS:
            converted_job_ids.append(job_id)
            job_outcomes.append({"job_id": job_id, "kind": kind, "outcome": "closed_housekeeping"})
            continue

        if kind in KEEP_KINDS:
            aid, keep_work, err = _build_keep_work(db, job_id, created_ms)
            if err is not None:
                refused_jobs.append({"job_id": job_id, "kind": kind, "state": job_state, "reason": err})
                continue
            entry = ensure_series(aid)
            entry["state"] = "WORKING"
            entry["work"]["keep"] = keep_work
            converted_job_ids.append(job_id)
            step_counts["keep"] = step_counts.get("keep", 0) + 1
            job_outcomes.append({
                "job_id": job_id, "kind": kind, "anidb_id": aid, "outcome": f"work.keep:{keep_work['step']}"
            })
            continue

        if kind in SEARCH_KINDS:
            aid, _, _, _ = _resolve_job_anidb_and_infohash(db, job_id, event_id, payload)
            wanted_nums: list[int] = []
            if payload.get("anidb_episode_number") is not None:
                wanted_nums.append(int(payload["anidb_episode_number"]))
            # Also collect from episode_discovery_results for this job/anidb_id
            edr_rows = db.execute(
                "SELECT uep.anidb_id, edr.anidb_episode_number "
                "FROM episode_discovery_results edr "
                "JOIN user_evaluation_plans uep ON uep.plan_id = edr.plan_id "
                "WHERE edr.job_id = ? AND edr.status <> 'HAS_FILE'",
                (job_id,),
            ).fetchall()
            for r_aid, r_ep in edr_rows:
                aid = aid or int(r_aid)
                if int(r_ep) not in wanted_nums:
                    wanted_nums.append(int(r_ep))
            if aid is None:
                refused_jobs.append({
                    "job_id": job_id, "kind": kind, "state": job_state, "reason": "search job missing anidb_id"
                })
                continue
            if not wanted_nums:
                # Try user_evaluation_plans window for this anidb_id
                uep = db.execute(
                    "SELECT anchor_episode, backfill_count, following_count FROM user_evaluation_plans WHERE anidb_id=?",
                    (aid,),
                ).fetchone()
                if uep:
                    anchor, backfill, following = int(uep[0]), int(uep[1]), int(uep[2])
                    nums = set(range(1, backfill + 1)) | set(range(anchor, anchor + following))
                    wanted_nums = sorted(nums)
            entry = ensure_series(aid)
            if entry["state"] not in ("KEPT", "REJECTED"):
                entry["state"] = "WORKING"
            search = entry["work"].setdefault("search", {"wanted": [], "tried": {}})
            search["wanted"] = sorted(set(search["wanted"]) | set(wanted_nums))
            search["due_ms"] = now_ms
            converted_job_ids.append(job_id)
            step_counts["search"] = step_counts.get("search", 0) + 1
            job_outcomes.append({
                "job_id": job_id, "kind": kind, "anidb_id": aid, "outcome": f"work.search({len(wanted_nums)} eps)"
            })
            continue

        if kind not in STEP_BY_KIND:
            refused_jobs.append({
                "job_id": job_id, "kind": kind, "state": job_state, "reason": f"unsupported job kind: {kind}"
            })
            continue

        target_step = STEP_BY_KIND[kind]
        aid, infohash, release_name, raw_body = _resolve_job_anidb_and_infohash(db, job_id, event_id, payload)
        if aid is None:
            refused_jobs.append({
                "job_id": job_id, "kind": kind, "state": job_state, "reason": "cannot resolve anidb_id"
            })
            continue
        if not infohash:
            refused_jobs.append({
                "job_id": job_id, "kind": kind, "state": job_state, "reason": "cannot resolve torrent infohash"
            })
            continue

        entry = ensure_series(aid)
        if entry["state"] not in ("KEPT", "REJECTED"):
            entry["state"] = "WORKING"

        identity = _identity_from_event(db, event_id, aid)
        qb = None
        files = []
        ep_codes = []

        # Single-episode details from acquisition_plans / reservations
        ap_row = db.execute(
            "SELECT ap.plan_id, ap.submission_marker, ap.manifest_json, er.tvdb_season, er.tvdb_episode, "
            "rv.anidb_episode_number, er.anidb_episode_id, er.tvdb_episode_id, mv.evidence_json "
            "FROM acquisition_plans ap "
            "JOIN episode_reservations er ON er.reservation_id = ap.reservation_id "
            "JOIN release_verifications rv ON rv.verification_id = er.verification_id "
            "LEFT JOIN media_verifications mv ON mv.plan_id = ap.plan_id "
            "WHERE ap.info_hash = ? AND er.anidb_id = ?",
            (infohash, aid),
        ).fetchone()
        if ap_row:
            plan_id, marker, ap_manifest, tvdb_season, tvdb_ep, anidb_ep_num, anidb_ep_id, tvdb_ep_id, ev_json = ap_row
            qb = _qb_state_from_single(db, plan_id, marker)
            if tvdb_season is not None and tvdb_ep is not None:
                code = _ep_code(tvdb_season, tvdb_ep)
                ep_codes.append(code)
                files.append(_single_file_entry(
                    ap_manifest, code, int(anidb_ep_num), int(anidb_ep_id), int(tvdb_ep_id),
                    evidence_json=ev_json,
                ))
        else:
            # Batch details
            bv_row = db.execute(
                "SELECT bv.batch_id, buep.plan_id "
                "FROM batch_verifications bv "
                "LEFT JOIN batch_user_evaluation_plans buep ON buep.batch_id = bv.batch_id "
                "WHERE bv.info_hash = ? AND bv.anidb_id = ?",
                (infohash, aid),
            ).fetchone()
            if bv_row:
                batch_id, bue_plan_id = bv_row
                if bue_plan_id is not None:
                    qb = _qb_state_from_batch(db, bue_plan_id)
                bvf_rows = db.execute(
                    "SELECT bvf.file_index, bvf.file_path, bvf.file_size_bytes, "
                    "bvf.anidb_episode_number, bvf.anidb_episode_id, bvf.tvdb_season, bvf.tvdb_episode, bvf.tvdb_episode_id "
                    "FROM batch_verified_files bvf "
                    "WHERE bvf.batch_id = ? ORDER BY bvf.file_index",
                    (batch_id,),
                ).fetchall()
                for f_idx, f_path, f_size, a_num, a_id, t_sea, t_ep, t_id in bvf_rows:
                    code = _ep_code(t_sea, t_ep)
                    ep_codes.append(code)
                    f_dict = {
                        "index": int(f_idx),
                        "path": f_path,
                        "size": int(f_size),
                        "episode": code,
                        "anidb_episode_number": int(a_num),
                        "anidb_episode_id": int(a_id),
                        "tvdb_episode_id": int(t_id),
                    }
                    if bue_plan_id is not None:
                        bem = db.execute(
                            "SELECT evidence_json FROM batch_episode_media WHERE plan_id=? AND file_index=?",
                            (bue_plan_id, f_idx),
                        ).fetchone()
                        if bem:
                            ev = json.loads(bem[0])
                            f_dict["media"] = ev
                            if ev.get("path"):
                                f_dict["local_path"] = ev["path"]
                    files.append(f_dict)

        t = ensure_torrent(
            entry, infohash, release_name, raw_body, identity, target_step, t_state, why=why, qb=qb
        )
        # If torrent already existed (e.g. from staged), don't regress a done torrent unless not done
        if t["step"] != "done":
            t["step"] = target_step
            t["state"] = t_state
            t["why"] = why
            if qb is not None:
                t["qb"] = qb
            for c in ep_codes:
                if c not in t["episodes"]:
                    t["episodes"].append(c)
            if files and not t["files"]:
                t["files"] = files

        for c in ep_codes:
            if c not in entry["episodes"]:
                ep_st = "downloading" if target_step == "download" else ("verified" if target_step == "check_media" else "reserved")
                entry["episodes"][c] = {"state": ep_st, "torrent": infohash, "file": None}

        converted_job_ids.append(job_id)
        step_counts[target_step] = step_counts.get(target_step, 0) + 1
        job_outcomes.append({
            "job_id": job_id, "kind": kind, "anidb_id": aid, "infohash": infohash,
            "outcome": f"{target_step} ({t_state})",
        })

    converted = []
    for aid in sorted(series_map):
        entry = series_map[aid]
        converted.append({
            "anidb_id": aid,
            "title": entry["title"],
            "state": entry["state"],
            "wake_at_ms": entry["wake_at_ms"],
            "torrents": list(entry["torrents"].values()),
            "episodes": entry["episodes"],
            "work": entry["work"],
        })

    return {
        "already_cut_over": False,
        "unfinished_jobs": len(unfinished),
        "series_to_convert": converted,
        "converted_job_ids": converted_job_ids,
        "job_outcomes": job_outcomes,
        "refused_jobs": refused_jobs,
        "step_counts": step_counts,
    }


def _apply_079_if_needed(db: sqlite3.Connection) -> bool:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 79:
        return False
    if version != 78:
        raise ValueError(f"Expected database schema v78 or v79, found v{version}")
    sql_079 = (migrations.SQL_DIR / "079_series.sql").read_text()
    db.executescript(sql_079)
    return True


def execute_cutover(path: Path, *, apply: bool = False) -> dict:
    """Preview or apply the one-time cutover on the SQLite database."""
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        plan = inspect_cutover(db)
        report = {
            "already_cut_over": plan["already_cut_over"],
            "series_count": len(plan["series_to_convert"]),
            "converted_jobs": len(plan["converted_job_ids"]),
            "refused_jobs": plan["refused_jobs"],
            "step_counts": plan["step_counts"],
            "job_outcomes": plan["job_outcomes"],
            "migrated_to_v79": False,
            "backup": None,
            "applied": False,
        }
        if not apply or plan["already_cut_over"]:
            return report

        active = services_stopped()
        if active:
            raise RuntimeError(f"Services must be stopped before cutover: {', '.join(active)}")

        report["backup"] = str(backup(db, path))
        report["migrated_to_v79"] = _apply_079_if_needed(db)

        now = time.time_ns() // 1_000_000
        db.execute("BEGIN IMMEDIATE")
        try:
            for item in plan["series_to_convert"]:
                aid = item["anidb_id"]
                recent = [f"series {aid} {item['title']} · converted at cutover"]
                db.execute(
                    "INSERT INTO series(anidb_id, title, state, wake_at_ms, torrents_at_once, "
                    "torrents_json, episodes_json, recent_json, lease_token, lease_expires_at_ms, "
                    "created_at_ms, updated_at_ms, work_json) "
                    "VALUES(?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, ?, ?, ?) "
                    "ON CONFLICT(anidb_id) DO UPDATE SET "
                    "title=excluded.title, state=excluded.state, wake_at_ms=excluded.wake_at_ms, "
                    "torrents_json=excluded.torrents_json, episodes_json=excluded.episodes_json, "
                    "work_json=excluded.work_json, updated_at_ms=excluded.updated_at_ms",
                    (
                        aid,
                        item["title"],
                        item["state"],
                        item["wake_at_ms"],
                        json.dumps(item["torrents"], separators=(",", ":")),
                        json.dumps(item["episodes"], separators=(",", ":")),
                        json.dumps(recent, separators=(",", ":")),
                        now,
                        now,
                        json.dumps(item["work"], separators=(",", ":")),
                    ),
                )

            for job_id in plan["converted_job_ids"]:
                db.execute(
                    "UPDATE jobs SET state='CANCELED', last_reason_code='CUTOVER_CONVERTED', "
                    "activity_code=NULL, lease_token=NULL, lease_expires_at_ms=NULL, updated_at_ms=? "
                    "WHERE job_id=?",
                    (now, job_id),
                )

            db.commit()
        except BaseException:
            db.rollback()
            raise

        report["applied"] = True
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/anime-discovery"))
    parser.add_argument("--database", type=Path, help="Override database path")
    parser.add_argument("--apply", action="store_true", help="Back up DB, apply 079 if needed, convert & close jobs")
    args = parser.parse_args(argv)

    db_path = args.database or configured_database(args.config_dir)
    report = execute_cutover(db_path, apply=args.apply)
    print(json.dumps(report, indent=2))
    if not args.apply and not report["already_cut_over"]:
        print("Preview only: rerun with --apply after stopping services.")
    return 1 if report["refused_jobs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
