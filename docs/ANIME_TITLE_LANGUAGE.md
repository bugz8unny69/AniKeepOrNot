# Anime title language (NextGen)

Settings → anime → **Anime title language** offers exactly **English** (default)
or **Japanese (Romaji)**. This changes display names only. The option applies
to subsequent API reads without a service restart. AniDB/TVDB IDs, episode
placement, release matching, recognized aliases, staging paths and filenames
are never renamed by this feature.

The AniDB anime response already supplies typed and language-tagged titles.
NextGen now records available English (`en`) and romanized Japanese
(`x-jat`) titles alongside the verified series identity. Metadata is held in
`anime_title_metadata` keyed by the **AniDB ID**, so distinct sequel/cour
entries sharing a TVDB series ID cannot accidentally share a title. Historical
`identity_results` snapshots and `recognized_seasons.display_title` remain
untouched. Missing translations display the existing canonical title instead.

## Upgrades and existing staged anime

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
non-production environments. The script inspects unique anime IDs referenced
by individual and batch staged media, including those later Kept. It requests
metadata through the **existing throttled AniDB client and provider cache**,
backs up the SQLite database before applying changes, and only fills missing
English/Romaji title values. It is safe to rerun. Provider problems and missing
English translations are reported for manual review. No media paths,
recognition mappings, or downloads are changed. No new job engine is involved.

Focused offline tests:
```sh
python3 -m unittest src.nextgen.tests.test_anime_title_language src.nextgen.tests.test_series_intake src.nextgen.tests.test_configuration_schema
node --check src/nextgen/dashboard/static/dashboard.js
```

CI is intentionally not run for this change.
