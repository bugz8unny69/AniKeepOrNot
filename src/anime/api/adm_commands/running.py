"""adm running: what is being worked on right now."""
import argparse
import json
import sqlite3
import sys

from src.anime.configuration import cli_config_dir

NAME = 'running'
SUMMARY = 'What is being worked on right now'


def connect(config_dir):
    from src.anime.configuration import ConfigurationStore
    try:
        db_path = ConfigurationStore(config_dir).read().value('storage.database_path')
    except Exception:
        db_path = '/var/lib/akon/akon.sqlite'
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv, prog):
    parser = argparse.ArgumentParser(prog=prog, description=SUMMARY)
    parser.add_argument('--config-dir', default=cli_config_dir(), help='Configuration directory')
    parser.add_argument('--json', action='store_true', default=False, help='Print as JSON')
    args = parser.parse_args(argv)

    try:
        db = connect(args.config_dir)
    except Exception as exc:
        print(f"Cannot open the database ({exc.__class__.__name__}: {exc}).", file=sys.stderr)
        return 1

    try:
        series_rows = db.execute(
            "SELECT anidb_id, title, state, wake_at_ms, torrents_at_once, "
            "       torrents_json, episodes_json, recent_json, updated_at_ms "
            "FROM series WHERE state = 'WORKING' "
            "ORDER BY updated_at_ms DESC"
        ).fetchall()

        series_list = []
        for r in series_rows:
            series_list.append({
                'anidb_id': r['anidb_id'],
                'title': r['title'],
                'state': r['state'],
                'wake_at_ms': r['wake_at_ms'],
                'torrents': json.loads(r['torrents_json']) if r['torrents_json'] else [],
                'episodes': json.loads(r['episodes_json']) if r['episodes_json'] else {},
                'recent': json.loads(r['recent_json']) if r['recent_json'] else [],
                'updated_at_ms': r['updated_at_ms'],
            })

        waiting_rows = db.execute(
            "SELECT request_id, infohash, received_at_ms, tries "
            "FROM identity_requests WHERE outcome IS NULL "
            "ORDER BY received_at_ms ASC LIMIT 50"
        ).fetchall()
        waiting_list = [
            {'request_id': w['request_id'], 'infohash': w['infohash'], 'received_at_ms': w['received_at_ms'], 'tries': w['tries']}
            for w in waiting_rows
        ]

        if args.json:
            print(json.dumps({'series': series_list, 'waiting_identity': waiting_list}, indent=2))
            return 0

        print("=== Working Series ===")
        if not series_list:
            print("  (no series currently working)")
        for s in series_list:
            print(f"Series {s['anidb_id']}: {s['title']}")
            active_torrents = [t for t in s['torrents'] if t.get('state') in ('waiting', 'working')]
            if not active_torrents:
                print("  (waiting for releases or downloads)")
            for t in active_torrents:
                name = t.get('release_name', t.get('infohash', 'unknown'))
                pct = int(t.get('progress', 0.0) * 100)
                step = t.get('step', 'unknown')
                eps = f" [{', '.join(t.get('episodes', []))}]" if t.get('episodes') else ""
                print(f"  · {name}{eps}: step {step}, {pct}%")

        print("\n=== Releases Waiting on AniDB ===")
        if not waiting_list:
            print("  (none)")
        for w in waiting_list:
            h = w['infohash'] or 'unknown'
            print(f"  · request #{w['request_id']}: {h} (tries: {w['tries']})")

        return 0
    finally:
        db.close()
