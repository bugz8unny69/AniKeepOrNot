#!/usr/bin/env python3
"""One-time cutover from old per-step jobs to the series table (SJ-9).

Preview by default. With --apply:
1. Backs up the SQLite database alongside the original (0600).
2. Ensures the `series` table (v78) exists.
3. Converts unfinished work, staged publications, and review decisions into
   `series` rows (one row per AniDB ID) with fresh history.
4. Drops the old jobs, job_history, job_operations, and per-step tables,
   then runs VACUUM to reclaim disk space.
5. Optionally (--migrate-qb) moves qBittorrent torrents from the old
   `anime-discovery` category/folders to `akon` and updates saved DB paths.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixes.cleanup_legacy_not_listed import backup, configured_database, open_database  # noqa: E402

SERIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS series (
 anidb_id INTEGER PRIMARY KEY,
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('WORKING','ASLEEP','KEPT','REJECTED')),
 wake_at_ms INTEGER,
 torrents_at_once INTEGER NOT NULL DEFAULT 1,
 torrents_json TEXT NOT NULL,
 episodes_json TEXT NOT NULL,
 recent_json TEXT NOT NULL,
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_state_wake ON series(state, wake_at_ms);
"""

OLD_TABLES_TO_DROP = (
    # Child/step tables first
    "job_history",
    "job_operation_members",
    "cancellation_cleanup",
    "staging_cleanup_artifacts",
    "management_actions",
    "release_verifications",
    "trial_plans",
    "trial_evaluations",
    "episode_reservations",
    "acquisition_plans",
    "acquisition_budgets",
    "torrent_submissions",
    "torrent_transfers",
    "media_verifications",
    "staging_publications",
    "keep_imports",
    "batch_verifications",
    "batch_verified_files",
    "batch_trial_plans",
    "batch_trial_files",
    "batch_episode_reservations",
    "batch_torrent_submissions",
    "batch_torrent_transfers",
    "batch_probe_media",
    "batch_torrent_unlocks",
    "batch_probe_stops",
    "batch_episode_media",
    "batch_staging_publications",
    "batch_keep_imports",
    "batch_download_cleanup",
    "batch_keep_cleanup",
    "batch_submission_inventories",
    "batch_cancellation_cleanup",
    "batch_metadata_admissions",
    "batch_candidates",
    "batch_candidate_scopes",
    "batch_candidate_events",
    "batch_candidate_stops",
    "prowlarr_search_snapshots",
    "sonarr_backfill_results",
    "sonarr_episode_selections",
    "sonarr_episode_candidates",
    "sonarr_magnet_intents",
    "manual_release_submissions",
    "manual_release_archive",
    "user_evaluation_checks",
    "event_history",
    # Core job tables
    "jobs",
    "job_operations",
)

KIND_TO_STEP = {
    "VERIFY_RELEASE": "check_release",
    "DISCOVER_EPISODE_RELEASES": "check_release",
    "DISCOVER_BATCH_PROWLARR": "check_release",
    "RESERVE_EPISODE": "reserve_episodes",
    "RESERVE_BATCH": "reserve_episodes",
    "PREPARE_ACQUISITION": "download",
    "SUBMIT_TORRENT": "download",
    "DOWNLOAD_TORRENT": "download",
    "SUBMIT_BATCH": "download",
    "TRANSFER_BATCH": "download",
    "VERIFY_MEDIA": "check_media",
    "VERIFY_BATCH_PROBE": "check_media",
    "VERIFY_BATCH_EPISODE": "check_media",
    "TRANSCODE_MEDIA": "transcode",
    "STAGE_USER_EVALUATION_MEDIA": "stage",
    "STAGE_BATCH_EPISODE": "stage",
    "SELECT_USER_EVALUATION": "stage",
    "EVALUATE_BATCH": "stage",
    "KEEP_MEDIA": "stage",
    "KEEP_BATCH": "stage",
}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _ep_code(season: int | None, episode: int | None, anidb_ep: int | None) -> str:
    if season is not None and episode is not None:
        return f"S{int(season):02d}E{int(episode):02d}"
    if episode is not None:
        return f"E{int(episode):02d}"
    if anidb_ep is not None:
        return f"E{int(anidb_ep):02d}"
    return "E01"


def _resolve_title(db: sqlite3.Connection, anidb_id: int) -> str:
    for query in (
        "SELECT COALESCE(english_title, romaji_title) FROM anime_title_metadata WHERE anidb_id=?",
        "SELECT title FROM identified_anime WHERE anidb_id=?",
        "SELECT title FROM recognized_series WHERE anidb_id=?",
    ):
        try:
            row = db.execute(query, (anidb_id,)).fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
    return f"AniDB {anidb_id}"


def _job_anidb_id(db: sqlite3.Connection, job_id: int, payload: dict) -> int | None:
    if payload.get("anidb_id") is not None:
        return int(payload["anidb_id"])
    for table, col in (
        ("release_verifications", "job_id"),
        ("episode_reservations", "owner_job_id"),
        ("batch_episode_reservations", "owner_job_id"),
    ):
        if _table_exists(db, table):
            try:
                row = db.execute(f"SELECT anidb_id FROM {table} WHERE {col}=?", (job_id,)).fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            except sqlite3.Error:
                pass
    if _table_exists(db, "job_operation_members") and _table_exists(db, "job_operations"):
        try:
            row = db.execute(
                "SELECT o.anidb_id FROM job_operation_members m "
                "JOIN job_operations o USING(operation_id) WHERE m.job_id=?",
                (job_id,),
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except sqlite3.Error:
            pass
    return None


def inspect_cutover(db: sqlite3.Connection) -> dict:
    """Inspect the database and compute the series rows to create/update."""
    if not _table_exists(db, "jobs"):
        return {
            "already_cut_over": True,
            "unfinished_jobs": 0,
            "series_to_convert": [],
            "old_history_rows": 0,
            "old_tables_present": [t for t in OLD_TABLES_TO_DROP if _table_exists(db, t)],
        }

    old_history_rows = 0
    if _table_exists(db, "job_history"):
        old_history_rows = db.execute("SELECT count(*) FROM job_history").fetchone()[0]

    unfinished = db.execute(
        "SELECT job_id, event_id, kind, state, payload_json, last_reason_code, created_at_ms, updated_at_ms "
        "FROM jobs WHERE state NOT IN ('DONE', 'CANCELED') ORDER BY job_id"
    ).fetchall()

    series_map: dict[int, dict] = {}

    def ensure_series(aid: int) -> dict:
        if aid not in series_map:
            series_map[aid] = {
                "anidb_id": aid,
                "title": _resolve_title(db, aid),
                "state": "ASLEEP",
                "torrents": {},
                "episodes": {},
            }
        return series_map[aid]

    # 1. Review decisions
    if _table_exists(db, "anime_review_decisions"):
        for aid, decision in db.execute(
            "SELECT anidb_id, decision FROM anime_review_decisions"
        ).fetchall():
            if aid is None:
                continue
            entry = ensure_series(int(aid))
            if decision == "ACCEPT":
                entry["state"] = "KEPT"
            elif decision == "REJECT":
                entry["state"] = "REJECTED"

    # 2. Staged publications (single & batch)
    for pub_table in ("staging_publications", "batch_staging_publications"):
        if _table_exists(db, pub_table):
            try:
                rows = db.execute(
                    f"SELECT manifest_json, receipts_json FROM {pub_table} WHERE state='PUBLISHED'"
                ).fetchall()
                for manifest_json, receipts_json in rows:
                    manifest = json.loads(manifest_json or "{}")
                    receipts = json.loads(receipts_json or "{}")
                    aid = manifest.get("anidb_id") or receipts.get("anidb_id")
                    if aid is None:
                        continue
                    entry = ensure_series(int(aid))
                    code = _ep_code(
                        manifest.get("tvdb_season"),
                        manifest.get("tvdb_episode"),
                        manifest.get("anidb_episode_number") or manifest.get("anidb_episode_id"),
                    )
                    staged_path = (
                        manifest.get("staged_path")
                        or manifest.get("media_path")
                        or receipts.get("staged_path")
                        or receipts.get("media_path")
                    )
                    ep_state = "kept" if entry["state"] == "KEPT" else "staged"
                    entry["episodes"][code] = {"state": ep_state, "torrent": "", "file": staged_path}
            except (sqlite3.Error, json.JSONDecodeError):
                pass

    # 3. Active episode reservations (single & batch)
    for res_table in ("episode_reservations", "batch_episode_reservations"):
        if _table_exists(db, res_table):
            try:
                rows = db.execute(
                    f"SELECT anidb_id, tvdb_season, tvdb_episode, anidb_episode_id FROM {res_table} WHERE state='ACTIVE'"
                ).fetchall()
                for aid, season, ep, anidb_ep in rows:
                    if aid is None:
                        continue
                    entry = ensure_series(int(aid))
                    code = _ep_code(season, ep, anidb_ep)
                    if code not in entry["episodes"]:
                        entry["episodes"][code] = {"state": "reserved", "torrent": "", "file": None}
            except sqlite3.Error:
                pass

    # 4. Unfinished jobs
    for job_id, event_id, kind, job_state, payload_json, reason_code, created_ms, updated_ms in unfinished:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        aid = _job_anidb_id(db, job_id, payload)
        if aid is None:
            continue
        entry = ensure_series(aid)
        if entry["state"] not in ("KEPT", "REJECTED"):
            entry["state"] = "WORKING"

        raw_body = None
        release_name = payload.get("release_name") or f"job-{job_id}"
        infohash = payload.get("info_hash") or payload.get("torrent_infohash") or ""
        if event_id is not None and _table_exists(db, "incoming_events"):
            ev = db.execute("SELECT raw_body FROM incoming_events WHERE event_id=?", (event_id,)).fetchone()
            if ev and ev[0]:
                try:
                    raw_str = ev[0].decode("utf-8", errors="replace") if isinstance(ev[0], bytes) else str(ev[0])
                    raw_body = raw_str
                    ev_json = json.loads(raw_str)
                    release_name = ev_json.get("release_name") or release_name
                    infohash = ev_json.get("torrent_infohash") or ev_json.get("info_hash") or infohash
                except Exception:
                    pass

        if not infohash and _table_exists(db, "acquisition_plans"):
            try:
                ap = db.execute(
                    "SELECT info_hash, manifest_json FROM acquisition_plans WHERE job_id=?", (job_id,)
                ).fetchone()
                if ap:
                    infohash = ap[0] or infohash
                    if ap[1]:
                        m = json.loads(ap[1])
                        release_name = m.get("name") or m.get("torrent_name") or release_name
            except (sqlite3.Error, json.JSONDecodeError):
                pass

        key = infohash or f"job-{job_id}"
        step = KIND_TO_STEP.get(kind, "check_release")
        t_state = "working" if job_state in ("PENDING", "RUNNING", "RETRY_WAIT") else "waiting"
        entry["torrents"][key] = {
            "infohash": infohash,
            "release_name": release_name,
            "raw_body": raw_body,
            "step": step,
            "state": t_state,
            "why": reason_code,
            "episodes": [],
            "files": [],
            "progress": 0.0,
            "submitted_at_ms": None,
        }

    converted = []
    for aid in sorted(series_map):
        entry = series_map[aid]
        converted.append({
            "anidb_id": aid,
            "title": entry["title"],
            "state": entry["state"],
            "torrents": list(entry["torrents"].values()),
            "episodes": entry["episodes"],
        })

    return {
        "already_cut_over": False,
        "unfinished_jobs": len(unfinished),
        "series_to_convert": converted,
        "old_history_rows": old_history_rows,
        "old_tables_present": [t for t in OLD_TABLES_TO_DROP if _table_exists(db, t)],
    }


def update_saved_paths(db: sqlite3.Connection, old_prefix: str = "anime-discovery", new_prefix: str = "akon") -> int:
    """Update saved file paths in series rows from old_prefix to new_prefix."""
    if not _table_exists(db, "series"):
        return 0
    updated = 0
    for aid, torrents_json, episodes_json in db.execute(
        "SELECT anidb_id, torrents_json, episodes_json FROM series"
    ).fetchall():
        new_t = torrents_json.replace(f"/{old_prefix}/", f"/{new_prefix}/")
        new_e = episodes_json.replace(f"/{old_prefix}/", f"/{new_prefix}/")
        if new_t != torrents_json or new_e != episodes_json:
            db.execute(
                "UPDATE series SET torrents_json=?, episodes_json=? WHERE anidb_id=?",
                (new_t, new_e, aid),
            )
            updated += 1
    return updated


def execute_cutover(
    path: Path,
    *,
    apply: bool = False,
    drop_old_tables: bool = True,
    rename_paths: bool = True,
) -> dict:
    """Run cutover preview or apply it to the SQLite database."""
    path = Path(path)
    with closing(open_database(path, readonly=not apply)) as db:
        plan = inspect_cutover(db)
        report = {
            "already_cut_over": plan["already_cut_over"],
            "unfinished_jobs": plan["unfinished_jobs"],
            "series_count": len(plan["series_to_convert"]),
            "old_history_rows": plan["old_history_rows"],
            "old_tables_to_drop": len(plan["old_tables_present"]) if drop_old_tables else 0,
            "paths_updated": 0,
            "backup": None,
            "applied": False,
        }
        if not apply or plan["already_cut_over"]:
            return report

        report["backup"] = str(backup(db, path))
        now = time.time_ns() // 1_000_000
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executescript(SERIES_TABLE_SQL)
            ver = db.execute("PRAGMA user_version").fetchone()[0]
            if ver < 78:
                db.execute("PRAGMA user_version = 78")

            for item in plan["series_to_convert"]:
                aid = item["anidb_id"]
                recent = [f"series {aid} {item['title']} · converted at cutover"]
                db.execute(
                    "INSERT INTO series(anidb_id, title, state, wake_at_ms, torrents_at_once, "
                    "torrents_json, episodes_json, recent_json, lease_token, lease_expires_at_ms, "
                    "created_at_ms, updated_at_ms) VALUES(?, ?, ?, NULL, 1, ?, ?, ?, NULL, NULL, ?, ?) "
                    "ON CONFLICT(anidb_id) DO UPDATE SET "
                    "title=excluded.title, state=excluded.state, "
                    "torrents_json=excluded.torrents_json, episodes_json=excluded.episodes_json, "
                    "updated_at_ms=excluded.updated_at_ms",
                    (
                        aid,
                        item["title"],
                        item["state"],
                        json.dumps(item["torrents"], separators=(",", ":")),
                        json.dumps(item["episodes"], separators=(",", ":")),
                        json.dumps(recent, separators=(",", ":")),
                        now,
                        now,
                    ),
                )

            if rename_paths:
                report["paths_updated"] = update_saved_paths(db)

            if drop_old_tables:
                for table in plan["old_tables_present"]:
                    db.execute(f'DROP TABLE IF EXISTS "{table}"')

            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

        if drop_old_tables and plan["old_tables_present"]:
            db.execute("VACUUM")

        report["applied"] = True
        return report


def migrate_qbittorrent(
    qb_url: str,
    username: str = "",
    password: str = "",
    old_category: str = "anime-discovery",
    new_category: str = "akon",
    old_path_segment: str = "/anime-discovery/",
    new_path_segment: str = "/akon/",
) -> dict:
    """Move torrents in qBittorrent from old_category/folders to new_category/folders."""
    import urllib.request
    import urllib.parse
    import http.cookiejar

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = qb_url.rstrip("/")

    if username or password:
        data = urllib.parse.urlencode({"username": username, "password": password}).encode()
        req = urllib.request.Request(f"{base}/api/v2/auth/login", data=data, method="POST")
        with opener.open(req, timeout=30) as resp:
            if resp.read().decode().strip() != "Ok.":
                raise RuntimeError("qBittorrent login failed")

    with opener.open(f"{base}/api/v2/torrents/info?category={urllib.parse.quote(old_category)}", timeout=30) as resp:
        torrents = json.loads(resp.read().decode())

    if not torrents:
        return {"moved_torrents": 0, "relocated_paths": 0}

    # Ensure target category exists
    try:
        cat_data = urllib.parse.urlencode({"category": new_category, "savePath": ""}).encode()
        req = urllib.request.Request(f"{base}/api/v2/torrents/createCategory", data=cat_data, method="POST")
        opener.open(req, timeout=30).close()
    except Exception:
        pass

    hashes = [t["hash"] for t in torrents if t.get("hash")]
    if hashes:
        set_cat = urllib.parse.urlencode({"hashes": "|".join(hashes), "category": new_category}).encode()
        req = urllib.request.Request(f"{base}/api/v2/torrents/setCategory", data=set_cat, method="POST")
        opener.open(req, timeout=30).close()

    relocated = 0
    for t in torrents:
        save_path = t.get("save_path") or ""
        if old_path_segment in save_path:
            new_save_path = save_path.replace(old_path_segment, new_path_segment)
            loc_data = urllib.parse.urlencode({"hashes": t["hash"], "location": new_save_path}).encode()
            req = urllib.request.Request(f"{base}/api/v2/torrents/setLocation", data=loc_data, method="POST")
            opener.open(req, timeout=30).close()
            relocated += 1

    return {"moved_torrents": len(hashes), "relocated_paths": relocated}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=None, help="Config directory (/etc/akon or /etc/anime-discovery)")
    parser.add_argument("--database", type=Path, help="Override database path")
    parser.add_argument("--apply", action="store_true", help="Back up DB and apply cutover")
    parser.add_argument("--keep-old-tables", action="store_true", help="Keep old job/step tables after conversion")
    parser.add_argument("--migrate-qb", action="store_true", help="Move qBittorrent category & folders to akon")
    parser.add_argument("--qb-url", help="qBittorrent base URL (for --migrate-qb)")
    parser.add_argument("--qb-user", default="", help="qBittorrent username")
    parser.add_argument("--qb-password", default="", help="qBittorrent password")
    args = parser.parse_args(argv)

    config_dir = args.config_dir
    if config_dir is None:
        config_dir = Path("/etc/akon") if Path("/etc/akon").exists() else Path("/etc/anime-discovery")

    try:
        db_path = args.database or configured_database(config_dir)
        report = execute_cutover(
            db_path,
            apply=args.apply,
            drop_old_tables=not args.keep_old_tables,
        )
        if args.apply and args.migrate_qb and args.qb_url:
            report["qbittorrent"] = migrate_qbittorrent(
                args.qb_url, username=args.qb_user, password=args.qb_password
            )
        print(json.dumps(report, indent=2))
        if not args.apply and not report["already_cut_over"]:
            print("Preview only: rerun with --apply to convert work and drop old tables.")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        print(f"Cutover refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
