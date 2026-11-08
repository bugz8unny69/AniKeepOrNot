#!/usr/bin/env python3
"""Backfill anime titles, repair published Jellyfin NFOs and request a scan.

Dry-run by default; --apply updates verified metadata with exclusive maintenance.
Never rename media, rewrite identity snapshots, or change recognition aliases.
Run after the NextGen schema/configuration migrations have been applied.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.anime.configuration import ConfigurationStore
from src.anime.jellyfin import request_refresh, RefreshConfiguration, RefreshUnavailable, RefreshReview
from src.anime.secrets import SecretStore, SecretStorageError
from fixes.nfo_title_repair import repair
from src.anime.maintenance import MaintenanceGate
from src.anime.providers.errors import (
    ProviderDeferred, ProviderInvalidResponse, ProviderUnavailable,
)
from src.anime.providers.http import Clients, Options
from src.anime.storage.job_queue import JobQueue
from src.anime.storage.sqlite import transaction, upsert_anime_titles


IDENTIFIED_ANIME = """
-- Include Tracking series even when no release has been staged yet.
SELECT a.anidb_id, a.title, t.english_title, t.romaji_title
FROM identified_anime a
LEFT JOIN anime_title_metadata t ON t.anidb_id = a.anidb_id
ORDER BY a.anidb_id
"""


def backup(database, connection):
    """SQLite online backup includes committed WAL content."""
    path = Path(database)
    target = path.with_name(path.name + ".before-title-backfill-" +
                            time.strftime("%Y%m%d-%H%M%S") + "-" +
                            str(os.getpid()) + ".sqlite")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(target) as destination:
            connection.backup(destination)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def lookup(client, aid):
    """Respect provider reservations; never bypass throttling."""
    waited = 0
    while True:
        try:
            return client.anime(aid)
        except ProviderDeferred as exc:
            if waited + exc.delay_ms > 120000:
                raise
            waited += exc.delay_ms
            time.sleep(exc.delay_ms / 1000)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="/etc/anime-discovery",
                        help="Directory containing the NextGen settings.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    mode.add_argument("--apply", action="store_true", help="Save missing titles")
    args = parser.parse_args(argv)

    config = ConfigurationStore(args.config_dir).read()
    database = config.value("storage.database_path")
    client = Clients(database, Options(config.value("anidb.client_name"),
                                      config.value("anidb.client_version"),
                                      config.value("providers.timeout_seconds")))
    counts = {"found": 0, "updated": 0, "unchanged": 0, "unresolved": 0, "failed": 0}
    gate = MaintenanceGate(database)
    # Editing published NFOs also changes durable staging receipts. Drain all
    # workers first; a failed repair leaves maintenance pending for safe retry.
    ticket = gate.request() if args.apply else None
    scope = gate.exclusive(ticket) if args.apply else gate.operation()
    planned_titles = {}
    with scope as lease:
        with JobQueue(database).connection() as db:
            records = db.execute(IDENTIFIED_ANIME).fetchall()
            counts["found"] = len(records)
            if args.apply:
                print("Database backup:", backup(database, db))
        for aid, canonical, existing_en, existing_ja in records:
            if existing_en and existing_ja:
                counts["unchanged"] += 1
                continue
            try:
                anime = lookup(client, aid)
            except (ProviderDeferred, ProviderUnavailable, ProviderInvalidResponse) as error:
                counts["failed"] += 1
                print(f"AniDB #{aid}: provider unavailable ({error.reason_code}); retry later")
                continue
            english = existing_en or anime.get("english_title")
            romaji = existing_ja or anime.get("romaji_title")
            if not english and not romaji:
                counts["unresolved"] += 1
                print(f"AniDB #{aid}: no verified English/Romaji title; needs review")
                continue
            if not english:
                counts["unresolved"] += 1
                print(f"AniDB #{aid}: no verified English title; existing name remains available")
            planned_titles[aid] = (english, romaji)
            if english == existing_en and romaji == existing_ja:
                counts["unchanged"] += 1
                continue
            print(f"AniDB #{aid}: {canonical} -> English: {english or '(missing)'}"
                  f" | Romaji: {romaji or '(missing)'}"
                  + (" [saved]" if args.apply else " [preview]"))
            if args.apply:
                with JobQueue(database).connection() as db, transaction(db):
                    upsert_anime_titles(db, aid, english, romaji)
            counts["updated"] += 1
        with JobQueue(database).connection() as db:
            nfos = repair(db, config, apply=args.apply, overrides=planned_titles)
        print(f"NFOs: {nfos['changed']} {'repaired' if args.apply else 'would change'}, "
              f"{nfos['unchanged']} unchanged, {nfos['missing']} already absent")
        if args.apply:
            lease.complete()
    refresh_failed = False
    if args.apply and nfos['changed']:
        if config.value('jellyfin.base_url'):
            try:
                request_refresh(config, SecretStore(args.config_dir))
                print("Jellyfin library refresh requested (204 accepted; indexing not confirmed)")
            except (RefreshConfiguration, RefreshUnavailable, RefreshReview, SecretStorageError) as exc:
                refresh_failed = True
                print(f"Jellyfin refresh failed: {exc.reason_code}; NFO repairs remain saved")
        else:
            print("Jellyfin address not configured; refresh skipped")

    label = "Applied" if args.apply else "Dry run"
    print(f"{label}: {counts['found']} identified anime, {counts['updated']} "
          f"{'updated' if args.apply else 'would update'}, "
          f"{counts['unchanged']} unchanged, {counts['unresolved']} unresolved, "
          f"{counts['failed']} provider failures")
    return 1 if counts["failed"] or refresh_failed else 0


if __name__ == "__main__":
    sys.exit(main())
