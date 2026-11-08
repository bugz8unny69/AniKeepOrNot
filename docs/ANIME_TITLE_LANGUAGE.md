# Anime title language (NextGen)

Settings → anime → **Anime title language** offers exactly **English** (default)
or **Japanese (Romaji)**. It selects dashboard names and NFO show titles written
for newly staged episodes. It applies to subsequent operations without restart.
AniDB/TVDB IDs, episode placement, release matching, recognized aliases,
staging paths and filenames remain unchanged.

The AniDB anime response already supplies typed and language-tagged titles.
NextGen now records available English (`en`) and romanized Japanese
(`x-jat`) titles alongside the verified series identity. Metadata is held in
`anime_title_metadata` keyed by the **AniDB ID**, so distinct sequel/cour
entries sharing a TVDB series ID cannot accidentally share a title. Historical
`identity_results` snapshots and `recognized_seasons.display_title` remain
untouched. Missing translations display the existing canonical title instead.

## Upgrades and existing identified anime

Apply the normal, explicit NextGen configuration **v12 → v13** and SQLite
**v51 → v52** migrations before using the new API or running the backfill
script. Follow the existing maintenance/backup procedure; the script does not
silently migrate the service.

From the repository root, run as the service account:

```sh
python3 fixes/backfill_anime_titles.py --dry-run
python3 fixes/backfill_anime_titles.py --apply
```

`--config-dir /etc/anime-discovery` is the default and can be overridden for
non-production environments. The script inspects every verified `identified_anime` entry, including
**Tracking** series without staged episodes, alongside Staged and Kept series.
It requests metadata through the **existing throttled AniDB client and provider cache**,
backs up the SQLite database before applying changes, and only fills missing
English/Romaji title values. It is safe to rerun. Provider problems and missing
English translations are reported for manual review. No media paths,
recognition mappings, or downloads are changed. No new job engine is involved.

Focused offline tests:
```sh
python3 -m unittest src.anime.tests.test_anime_title_language src.anime.tests.test_series_intake src.anime.tests.test_configuration_schema
node --check src/anime/dashboard/static/dashboard.js
```

CI validation is performed by the NextGen GitHub Actions workflow on PR #222 before merge.

## Existing Jellyfin NFO repair

The backfill script also previews and repairs already-published staging NFO
titles: `tvshow.nfo` uses the chosen series title and episode NFOs use it in
`showtitle`. Episode numbers and provider IDs are retained; directories,
video filenames and permanent Sonarr media are not renamed. It repairs the
existing NextGen file hashes, receipts and inode proofs so Keep/Reject cleanup
remains valid. It skips NFOs already removed after Keep, and fails closed for
unrecognized or externally modified NFOs. Applying requires exclusive
maintenance admission and creates a database backup.

After a successful application it reuses the configured Jellyfin
`/Library/Refresh` request. A successful scan request does **not** guarantee
that Jellyfin overwrote cached item titles or permanent-media metadata; verify
those independently. No direct Jellyfin item metadata edits are made.
