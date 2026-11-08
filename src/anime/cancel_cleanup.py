"""Cancellation cleanup: delete what this work owns. Nothing more.

Ownership is the ad-<marker> the app gave the work: its torrent is tagged
ad-<marker> and downloads into a folder named ad-<marker>. Cleanup
  1. deletes each such torrent and its files from qBittorrent,
  2. deletes each ad-<marker> download folder whatever it contains,
  3. deletes the staging copies this work wrote (video, episode NFO,
     temporary files) and a series folder left holding only tvshow.nfo.
Anything already gone counts as done, so running it again is harmless.
The torrent and its folder are deleted only together, and only when the
torrent is ours (ad-<marker> tag) and stored in its recorded folder, or is
already gone. A tagged torrent stored elsewhere stops for review. Also
nothing outside the staging folder it was written to, or inside the Keep
folder, is touched.
"""
import json
import os
import re
import shutil
from pathlib import PurePosixPath

MARKER = re.compile(r'[a-f0-9]{32}')
TEMPORARY = re.compile(r'\.ad-[a-f0-9]{32}\.partial')


def owned_torrents(db, operation_id):
    """(info_hash, marker, base_url, save_path, category, timeout) per torrent."""
    members = 'SELECT job_id FROM job_operation_members WHERE operation_id=?'
    rows = []
    rows += db.execute(
        "SELECT info_hash,marker,base_url,save_path,category,timeout_seconds "
        f"FROM magnet_intents WHERE job_id IN ({members}) "
        "AND state NOT IN ('EXTERNAL','RELEASED')", (operation_id,)).fetchall()
    rows += db.execute(
        "SELECT info_hash,ownership_marker,base_url,save_path,category,timeout_seconds "
        f"FROM batch_metadata_admissions WHERE verification_job_id IN ({members}) "
        "AND state NOT IN ('EXTERNAL','RELEASED')", (operation_id,)).fetchall()
    rows += db.execute(
        "SELECT p.info_hash,p.submission_marker,s.base_url,s.save_path,s.category,s.timeout_seconds "
        "FROM acquisition_plans p JOIN torrent_submissions s USING(plan_id) "
        f"WHERE p.job_id IN ({members}) AND s.state<>'EXTERNAL'", (operation_id,)).fetchall()
    rows += db.execute(
        "SELECT info_hash,ownership_marker,base_url,save_path,category,timeout_seconds "
        f"FROM batch_torrent_submissions WHERE reservation_job_id IN ({members}) "
        "AND state<>'EXTERNAL'", (operation_id,)).fetchall()
    seen, result = set(), []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0]); result.append(row)
    return result


def staging_files(db, operation_id):
    """(path, root) of staging files this work wrote, and series folders touched.

    root is the staging folder recorded when the file was written, so a later
    change of the staging setting does not matter.
    """
    members = 'SELECT job_id FROM job_operation_members WHERE operation_id=?'
    files, series = [], set()
    for (folder, name) in db.execute(
            "SELECT root_path,relative_path FROM staging_cleanup_artifacts "
            "WHERE operation_id=? AND phase<>'REMOVED'", (operation_id,)):
        if TEMPORARY.fullmatch(name):  # Our own temporary file name.
            files.append((str(PurePosixPath(folder) / name), folder))
    for table in ('staging_publications', 'batch_staging_publications'):
        for (manifest,) in db.execute(f'SELECT manifest_json FROM {table} WHERE job_id IN ({members})',
                                      (operation_id,)):
            try:
                plan = json.loads(manifest); root = plan['root']
                for entry in plan['entries']:
                    path = PurePosixPath(root) / entry['path']
                    if TEMPORARY.fullmatch(entry.get('temporary') or ''):
                        files.append((str(path.parent / entry['temporary']), root))
                    if entry.get('role') == 'series':
                        series.add((str(path.parent), root))
                    else:
                        files.append((str(path), root))
            except (ValueError, KeyError, TypeError):
                continue  # Unreadable plan: nothing provable to delete.
    return list(dict.fromkeys(files)), sorted(series)


def inside(path, root):
    try:
        PurePosixPath(os.path.normpath(path)).relative_to(PurePosixPath(os.path.normpath(root)))
        return bool(root) and os.path.normpath(path) != os.path.normpath(root)
    except ValueError:
        return False


def remove_download_folder(save_path, marker):
    """Delete the whole ad-<marker> folder. Only a folder with exactly that name."""
    if not MARKER.fullmatch(marker or '') or PurePosixPath(save_path).name != 'ad-' + marker:
        return
    try:
        if os.path.islink(save_path):
            os.unlink(save_path)
        else:
            shutil.rmtree(save_path)
    except FileNotFoundError:
        pass


def remove_staging(files, series, *, keep_root):
    for path, root in files:
        if not (inside(path, root) or os.path.dirname(path) == os.path.normpath(root)) \
                or (keep_root and inside(path, keep_root)):
            continue  # Outside its staging folder, or in Keep: not ours to delete.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for folder, root in series:
        if not inside(folder, root) or (keep_root and inside(folder, keep_root)):
            continue
        # Remove empty season folders; then the series folder if only tvshow.nfo is left.
        for current, dirs, names in os.walk(folder, topdown=False):
            if current != folder and not names and not os.listdir(current):
                os.rmdir(current)
        try:
            left = os.listdir(folder)
        except FileNotFoundError:
            continue
        if left in ([], ['tvshow.nfo']):
            if left: os.unlink(os.path.join(folder, 'tvshow.nfo'))
            os.rmdir(folder)
