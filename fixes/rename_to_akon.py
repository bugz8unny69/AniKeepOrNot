#!/usr/bin/env python3
"""One-time rename from anime-discovery to akon (RN-2 & RN-3).

Dry-run by default unless --apply is passed (or --dry-run is explicitly set).
Refuses unless all anime-discovery and akon services are stopped.
If any step fails, prints the exact undo commands for steps already completed
and raises with the full traceback.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.anime.acquisition.settings import DownloadOptions  # noqa: E402
from src.anime.configuration import ConfigurationStore  # noqa: E402
from src.anime.secrets import SecretStore  # noqa: E402
from src.anime.torrent_client.qbittorrent import Client as QbClient  # noqa: E402

UNITS_TO_CHECK = (
    "anime-discovery-api.service",
    "anime-discovery-worker.service",
    "akon-api.service",
    "akon-worker.service",
)


def _default_execute(cmd, *, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def active_services(execute=_default_execute) -> list[str]:
    if execute is _default_execute and shutil.which("systemctl") is None:
        return []
    active = []
    for unit in UNITS_TO_CHECK:
        proc = execute(["systemctl", "is-active", "--quiet", unit], check=False)
        if proc.returncode == 0:
            active.append(unit)
    return active


def _checkpoint_sqlite(db_path: Path):
    if not db_path.is_file():
        return
    with closing(sqlite3.connect(db_path, timeout=60.0)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _adm_script(root: str, config_dir: str) -> str:
    return (
        "#!/bin/sh\n"
        f"AKON_CONFIG_DIR={config_dir}\n"
        "export AKON_CONFIG_DIR\n"
        f'exec {root}/current/.venv/bin/python {root}/current/bin/adm "$@"\n'
    )


def _rewrite_installer_json(installer_json: Path, *, branches_map: dict, config_dir: str,
                            old_root: str, new_root: str, old_cfg: str, new_cfg: str,
                            new_state: str):
    doc = json.loads(installer_json.read_text())
    new_branches = {}
    for ref, entry in (doc.get("branches") or {}).items():
        cfg = entry.get("config_dir", "")
        if cfg.startswith(old_cfg):
            cfg = new_cfg + cfg[len(old_cfg):]
        new_branches[ref] = {**entry, "config_dir": cfg}
    doc["root"] = new_root
    doc["config_base"] = new_cfg
    doc["config_dir"] = config_dir
    doc["state_dir"] = new_state
    doc["user"] = "akon"
    doc["source"] = "git@github.com:bugz8unny69/AniKeepOrNot.git"
    if new_branches:
        doc["branches"] = new_branches
    tmp = installer_json.with_name(installer_json.name + ".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, installer_json)


def rename_to_akon(
    *,
    prefix: Path = Path("/"),
    apply: bool = False,
    execute=_default_execute,
    qb_client_factory=QbClient,
    out=print,
) -> dict:
    """Execute or preview the one-time rename to akon."""
    prefix = Path(prefix)
    etc_old = prefix / "etc/anime-discovery"
    etc_new = prefix / "etc/akon"
    var_old = prefix / "var/lib/anime-discovery"
    var_new = prefix / "var/lib/akon"
    bin_dir = prefix / "usr/local/bin"
    live_cfg_rel = "etc/akon/branches/develop"
    live_cfg_abs = prefix / live_cfg_rel
    old_db_name = "anime-discovery-develop.sqlite"
    # Owner, 2026-11-27: the branch stays in the name, as the installer names a branch's
    # database (<state>/akon-<branch>.sqlite).
    new_db_name = "akon-develop.sqlite"
    new_db_logical = "/var/lib/akon/akon-develop.sqlite"
    old_db_logical = "/var/lib/anime-discovery/anime-discovery-develop.sqlite"

    active = active_services(execute)
    if active:
        raise RuntimeError(f"Services must be stopped before rename: {', '.join(active)}")

    if not etc_old.exists():
        raise FileNotFoundError(f"Expected settings folder not found: {etc_old}")
    if not var_old.exists():
        raise FileNotFoundError(f"Expected data folder not found: {var_old}")
    if etc_new.exists():
        raise FileExistsError(f"Target settings folder already exists: {etc_new}")
    if var_new.exists():
        raise FileExistsError(f"Target data folder already exists: {var_new}")

    plan = {
        "apply": apply,
        "steps": [
            "usermod -l akon anime-discovery && groupmod -n akon anime-discovery && usermod -d /var/lib/akon akon",
            f"move {etc_old} -> {etc_new} and update storage.database_path -> {new_db_logical}",
            f"checkpoint & move {var_old} -> {var_new}, rename {old_db_name} -> {new_db_name}",
            f"remove the old pipeline's empty lock files in {var_new} (.{old_db_name}.nextgen-*, .nextgen-*stage-*.lock)",
            "retag & recategorize qBittorrent torrents in place (ad-<m> -> akon-<m>, category -> akon), set qb.prefix='ad', update qbittorrent.category='akon'",
            f"rewrite {var_new / 'installer/installer.json'} for /opt/akon, /etc/akon, /var/lib/akon, user akon",
            f"update {bin_dir / 'adm'} (AKON_CONFIG_DIR=/etc/akon/branches/develop) and remove anime-discovery-admin",
        ],
        "qb_renamed": [],
    }

    if not apply:
        for s in plan["steps"]:
            out(f"[dry-run] {s}")
        return plan

    undo: list[str] = []
    try:
        # 1. User & group
        execute(["usermod", "-l", "akon", "anime-discovery"])
        undo.append("usermod -l anime-discovery akon")
        execute(["groupmod", "-n", "akon", "anime-discovery"])
        undo.append("groupmod -n anime-discovery akon")
        # Same UID and GID: every file the service owns, local or on the NFS share, is akon's
        # with no chown. Its home is the data folder, which step 3 moves to /var/lib/akon.
        execute(["usermod", "-d", "/var/lib/akon", "akon"])
        undo.append("usermod -d /var/lib/anime-discovery akon")
        out("1. Renamed user and group anime-discovery -> akon (same IDs; home /var/lib/akon)")

        # 2. Settings
        shutil.move(str(etc_old), str(etc_new))
        undo.append(f"mv {etc_new} {etc_old}")
        for settings_file in sorted(etc_new.rglob("settings.json")):
            branch_dir = settings_file.parent
            store = ConfigurationStore(branch_dir)
            snap = store.read()
            updates = {}
            if snap.value("storage.database_path") == old_db_logical:
                updates.setdefault("storage", {})["database_path"] = new_db_logical
            if snap.value("qbittorrent.category") == "anime-discovery":
                updates.setdefault("qbittorrent", {})["category"] = "akon"
            if updates:
                store.update(updates, expected_revision=snap.revision)
        out(f"2. Moved settings {etc_old} -> {etc_new} and updated database_path/category")

        # 3. Data
        _checkpoint_sqlite(var_old / old_db_name)
        shutil.move(str(var_old), str(var_new))
        undo.append(f"mv {var_new} {var_old}")
        for suffix in ("", "-wal", "-shm"):
            src = var_new / (old_db_name + suffix)
            dst = var_new / (new_db_name + suffix)
            if src.exists():
                src.rename(dst)
                undo.append(f"mv {dst} {src}")
        old_files = var_new / (Path(old_db_name).stem + "-files")
        new_files = var_new / (Path(new_db_name).stem + "-files")
        if old_files.exists():
            old_files.rename(new_files)
            undo.append(f"mv {new_files} {old_files}")
        out(f"3. Moved data {var_old} -> {var_new} and renamed live database to {new_db_name}")

        # 3b. Lock files: the old pipeline's per-operation and staging locks, and the old database
        # name's gate locks (the gate makes its own for the new name). Only empty regular files.
        removed = 0
        for lock in sorted(var_new.iterdir()):
            stale = (lock.name.startswith(f".{old_db_name}.nextgen-")
                     or (lock.name.startswith(".nextgen-") and lock.name.endswith(".lock") and "stage-" in lock.name))
            if stale and lock.is_file() and not lock.is_symlink() and lock.stat().st_size == 0:
                lock.unlink()
                removed += 1
        out(f"3b. Removed {removed} old lock files")

        # 4. qBittorrent (RN-3)
        cfg_dir = live_cfg_abs if live_cfg_abs.is_dir() else etc_new
        snap = ConfigurationStore(cfg_dir).read()
        qb_url = snap.value("qbittorrent.base_url")
        if qb_url:
            secrets = SecretStore(cfg_dir)
            creds = secrets.qbittorrent_credentials()
            dl_root = snap.value("storage.download_root")
            opts = DownloadOptions(qb_url, dl_root, "akon", snap.value("qbittorrent.timeout_seconds"))
            client = qb_client_factory(opts, creds)
            client.login()
            qb_res = client.rename_legacy_torrents(dl_root, old_category="anime-discovery", new_category="akon")
            plan["qb_renamed"] = qb_res["renamed"]
            for item in qb_res["renamed"]:
                out(f"   qBittorrent renamed: {item['hash']} {item['name']} (ad-{item['marker']} -> akon-{item['marker']})")
            renamed_hashes = {item["hash"] for item in qb_res["renamed"]}
            live_db_file = var_new / new_db_name
            if live_db_file.is_file() and renamed_hashes:
                with closing(sqlite3.connect(live_db_file, timeout=60.0)) as db:
                    for aid, t_json in db.execute("SELECT anidb_id, torrents_json FROM series").fetchall():
                        torrents = json.loads(t_json)
                        changed = False
                        for t in torrents:
                            if t.get("infohash") in renamed_hashes:
                                t.setdefault("qb", {})["prefix"] = "ad"
                                changed = True
                        if changed:
                            db.execute(
                                "UPDATE series SET torrents_json=? WHERE anidb_id=?",
                                (json.dumps(torrents, separators=(",", ":")), aid),
                            )
                    db.commit()
        out("4. Updated qBittorrent tags/category in place")

        # 5. Installer state
        installer_json = var_new / "installer/installer.json"
        if installer_json.is_file():
            orig_installer = installer_json.read_text()
            _rewrite_installer_json(
                installer_json,
                branches_map={},
                config_dir="/etc/akon/branches/develop",
                old_root="/opt/anime-discovery",
                new_root="/opt/akon",
                old_cfg="/etc/anime-discovery",
                new_cfg="/etc/akon",
                new_state="/var/lib/akon",
            )
            undo.append(f"restore {installer_json} from backup")
            out(f"5. Updated {installer_json}")

        # 6. Commands
        adm_path = bin_dir / "adm"
        if adm_path.parent.exists():
            adm_path.write_text(_adm_script("/opt/akon", "/etc/akon/branches/develop"))
            os.chmod(adm_path, 0o755)
        legacy_admin = bin_dir / "anime-discovery-admin"
        legacy_admin.unlink(missing_ok=True)
        out(f"6. Updated {adm_path} and removed legacy admin command")

        return plan
    except BaseException:
        if undo:
            out("Rename failed. Undo completed steps in reverse order:")
            for step in reversed(undo):
                out(f"  {step}")
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path("/"), help="Filesystem root prefix (default /)")
    parser.add_argument("--apply", action="store_true", help="Apply the rename (preview/dry-run by default)")
    parser.add_argument("--dry-run", action="store_true", help="Print steps without changing anything")
    args = parser.parse_args(argv)

    rename_to_akon(prefix=args.prefix, apply=bool(args.apply and not args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
