# AniKeepOrNot — Project Status
Updated: 2026-10-18

## Repository checkpoint
- Private GitHub repository connected: `bugz8unny69/anime-discovery`.
- Initial documentation and safety guardrails are being established.
- Original user-supplied source archive `anime-discovery-current.tar.gz` is available in the current chat workspace (238 files, approximately 1.7 MiB extracted). **It has not yet been committed to GitHub.**
- No production-ready release or integrated end-to-end acceptance test has been completed.
- No change has been deployed to the VM by the assistant.

## Verified production preflight supplied by user
- `controller.db`: integrity OK; 8 series, 32 episodes; all 8 series are test fixtures.
- `inbox.db`: integrity OK; 1 queued untrusted notification, for Solo Leveling.
- No confirmed controller record for the EP2 notification.
- qBittorrent ownership and downloaded file existence have not been verified.

## Approved design
- Consolidate to `/var/lib/anime-discovery/anime-discovery.db` with WAL, foreign keys, consistent backup/migration, and quarantine of test fixtures.
- Preserve full runtime: identity worker, dashboard authentication, Autobrr webhook, controller, qBittorrent integration, staging, and admin CLI.
- Promote webhook notifications through canonical identity and episode processing. Hold ambiguous identities. Verify qBittorrent torrents and downloaded files; recover missing EP1 safely and idempotently; stage only verified files.
- Show-level Reject must persistently block rediscovery and remove owned Anime-Trial destination files. Optional torrent/data deletion is unchecked by default and only for verified-owned qBittorrent items; fail closed otherwise.
- CLI `/opt/anime-discovery/bin/anime-discovery-admin`: `series list`, `series inspect --series ID`, `history clear --series ID [--confirm]`, `series reset --series ID [--confirm]`, settings/doctor. Internal `series.id` is the ID. History clear preserves rejection; reset clears blocker and processing state but does not delete media or torrents.
- Remove hard-coded Solo Leveling EP2 pilot UI/compatibility paths.
- Installer must back up existing `/opt/anime-discovery` and consistent databases, stage replacement, build a fresh `/opt/anime-discovery/venv`, validate and roll back app and DB together on failure. Restrict writes to `/opt/anime-discovery`, `/etc/anime-discovery`, `/var/lib/anime-discovery`; no systemd changes without approval.

## Work completed
- Earlier diagnostic/admin prototypes, offline database migration rehearsals, and production packaging audit. These are not a cumulative deployable release.
- Packaging audit found 198 top-level Python files; static dependency traversal of six likely entrypoints reached 30 modules, **not** a safe production exclusion list.
- Source still references both `controller.db` and `inbox.db`.
- Broad historical test run reported 4 failures and 130 errors; focused passing tests do not establish full integration.

## Pending work
1. Import and review actual application source into GitHub after secret scan and baseline provenance check.
2. Inventory runtime entrypoints, imports, templates, service assumptions, and database readers/writers.
3. Implement unified database migration and notification promotion.
4. Implement end-to-end acquisition, missing EP1 recovery, ownership-safe deletion, rejection and reset workflows.
5. Validate dashboard authentication, admin CLI, integration tests, clean production packaging and rollback.
6. Release only after reproducible acceptance tests.

## Acceptance workflow
Fresh EP2 Autobrr webhook -> canonical series and episode -> verify or submit torrent -> recover missing EP1 -> verified staging -> Reject -> webhook blocked -> Inspect -> Clear History (still blocked) -> Reset -> fresh webhook accepted. Confirm idempotency and ownership safeguards.

## Next development action
Commit a reviewed, secret-sanitized source baseline; do not infer that a prototype archive is an integrated release. Work in feature branches and open pull requests for review.


## 2026-10-18 source baseline preparation
- Reviewed source snapshot prepared locally from original user archive: 198 top-level Python files, including development tests; 40 nested/historical/backup paths excluded from that snapshot.
- Seven additional token/password-like literal assignment findings manually classified as symbolic names (e.g. `ANIME_QB_PASSWORD = 'ANIME_QB_PASSWORD'`), not embedded credentials.
- No confirmed credential exposure in this scan; pattern-based checks do not guarantee absence of secrets.
- A manifest with SHA-256 for each included file was generated locally.
- **GitHub source import remains pending**: the GitHub integration can write individual files, but the local execution environment cannot directly reach GitHub. The prepared local archive has NOT been pushed to GitHub.
- Do not consider the repository a full application checkout or deployable release until source import is verified.
