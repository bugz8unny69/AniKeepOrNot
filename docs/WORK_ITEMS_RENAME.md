# Work items: the rename to `akon` (item 3)

Status: open. Written 2026-11-26. Branch: `feat/series-job` (PR 349; do not merge it).

## What this is

The owner decided (`docs/SERIES_JOBS_AND_RENAME.md`, "5. Rename") that everything named
`anime-discovery` becomes `akon`, once, at the cutover. The PR already renamed the defaults in
`bin/akon-install` and the code, but **nothing moves an existing installation**. An `--upgrade`:

- keeps the saved paths and user from the installer's state (`installer.json`), so it still uses
  `/opt/anime-discovery`, `/etc/anime-discovery` and the `anime-discovery` user;
- installs units named `akon-api.service` / `akon-worker.service` / `akon.target`; and
- never stops or removes the running `anime-discovery-*` units. Two sets of services would then run
  on one database.

This item makes the rename a real, one-time, reversible step.

## Production today (read 2026-11-26)

| What | Now |
|---|---|
| Units | `anime-discovery-api.service`, `anime-discovery-worker.service`, `anime-discovery.target` (in `/etc/systemd/system`) |
| User and group | `anime-discovery` (uid 994, gid 992), also in group `media` (gid 1002) |
| Application | `/opt/anime-discovery/current` → `/opt/anime-discovery/releases/2026-11-23-develop-d74c379a` (each release has its own `.venv`) |
| Settings | `/etc/anime-discovery/` (the NextGen branch), `/etc/anime-discovery/branches/{develop,refactor}/` (each has `settings.json`, `secrets.json`, `management-password.json`) |
| Data | `/var/lib/anime-discovery/anime-discovery-develop.sqlite` (live), `anime-discovery.sqlite`, `anime-discovery-refactor.sqlite` (other branches) |
| Installer state | `/var/lib/anime-discovery/installer/installer.json` (`root`, `config_base`, `config_dir`, `state_dir`, `user`, `source`, `branches`), `installation.json`, `unit-backups/` |
| Commands | `/usr/local/bin/akon-install`; `/usr/local/bin/adm` (sets `ANIME_DISCOVERY_CONFIG_DIR`); `/usr/local/bin/anime-discovery-admin` (old) |
| Source | `git@github.com:bugz8unny69/anime-discovery.git` (the repository moved to `AniKeepOrNot`; GitHub redirects) |
| Folders AKON uses | download root `/mnt/media/Torrents/anime`, staging `/mnt/media/Anime-Trial`, keep `/mnt/media/Anime`. **None contains the old name.** |
| qBittorrent | category `anime-discovery`. Each torrent AKON adds lives in `<download root>/ad-<marker>` and is tagged `ad-<marker>`. After the rename: tag `akon-<marker>`, category `akon`; old folders stay (RN-0). |
| nginx | `/etc/nginx/sites-available/default`: `location = /anime-discovery/api/v1/webhook` and `…/check`. **The owner edits nginx himself.** |
| Transcode agents | on other hosts (`10.1.1.2`, `10.1.3.24`), unit `transcode-agent.service`. They don't use the old name. |

Check these again before you start; read-only commands only.

## Rules (from the owner)

- Work on `feat/series-job`. Never touch `develop`, `main`, `refactor` or `NextGen`. Don't merge PR 349.
- Run the tests for what you change, never the whole suite.
- No catch-alls; errors raise with their full trace.
- **Nothing on production changes in this item.** Read-only checks over
  `ssh root@torrents.int.lhprojects.net` are fine. Rehearse in a temporary root (RN-6). The owner runs
  the real rename.
- Commits: the subject ends with `[skip ci]`; the message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push `feat/series-job` when tests pass.

## RN-0. The owner's answers, and the questions still open

1. **Folders, tags and category: answered (owner, 2026-11-26).** "We don't need to rename folders,
   just use the new save paths for future work. Changing tags and category should be harmless."
   - Existing torrents **stay where they are**, in `<download root>/ad-<marker>`. Nothing is moved.
   - Existing torrents get the tag `akon-<marker>` (instead of `ad-<marker>`) and the category `akon`.
   - New torrents get the folder `<download root>/akon-<marker>`, the tag `akon-<marker>` and the
     category `akon`.

   So after the rename, a torrent is AKON's when it has the tag `akon-<marker>`, the category `akon`,
   and its folder is `<download root>/akon-<marker>` or, for one added before the rename,
   `<download root>/ad-<marker>`. RN-3 implements this.

2. **The other branches' data: answered (owner, 2026-11-26).** Leave `anime-discovery.sqlite` (NextGen) and
   `anime-discovery-refactor.sqlite` alone; nothing runs them.
3. **How long the old web address keeps working: answered (owner, 2026-11-26).** Keep `/anime-discovery/`
   working until Autobrr points at `/akon/`, then retire it in a follow-up.

## Work items, in order

### RN-1. The installer stops and removes the old units (P1)

`bin/akon-install` only knows the new unit names (`SERVICES`, `akon.target`):

- When stopping services before setup (the `installed=[…]` list around `apply()`), include the old
  names `anime-discovery-api.service`, `anime-discovery-worker.service` and `anime-discovery.target`
  when they exist.
- After the new units start and the API answers, disable the old units and back up and remove their
  files (under `unit-backups/`, like the existing backups). Then `systemctl daemon-reload`.
- Record what was removed in `installation.json`, so `--rollback` restores the old units.
- Never leave both sets enabled. Test with the installer's existing test fixtures (`test_installer.py`,
  `test_branch_state.py`; they use a temporary `unit_dir`).

### RN-2. One rename step: user, folders, settings and installer state (P1)

Add an explicit mode, `akon-install --rename-to-akon` (or a `fixes/rename_to_akon.py` that the owner
runs). It is refused unless the services are stopped. With `--dry-run` it prints every step and
changes nothing. In order:

1. **User:** `usermod -l akon anime-discovery` and `groupmod -n akon anime-discovery`. The uid and gid
   stay the same, so every file stays owned and the `media` group membership is kept. Don't create a
   new user.
2. **Settings:** move `/etc/anime-discovery` to `/etc/akon` (with `branches/`). In each branch's
   `settings.json`, rewrite `storage.database_path` to the new path, through the settings store
   (`ConfigurationStore`), never by editing the JSON as text.
3. **Data:** move `/var/lib/anime-discovery` to `/var/lib/akon`. Rename the live database
   `anime-discovery-develop.sqlite` to `akon.sqlite` (the owner's name), with its `-wal`/`-shm`
   files, only after a checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`). Also move the series job's
   metadata folder next to it (`<database>-files/`, which takes the database's new stem:
   `akon-files/`). The other branches' databases follow RN-0 question 2.
4. **Application:** don't move `/opt/anime-discovery`; its virtualenvs contain absolute paths.
   Install a fresh release under `/opt/akon` (the normal install path), and remove
   `/opt/anime-discovery` only after the new one runs (keep it for `--rollback`).
5. **Installer state:** rewrite `installer.json` (`root`, `config_base`, `config_dir`, `state_dir`,
   `user`, `branches[*].config_dir`, `source` → `git@github.com:bugz8unny69/AniKeepOrNot.git`) and move
   it with the state folder.
6. **Commands:** regenerate `/usr/local/bin/adm` with `AKON_CONFIG_DIR`. Remove
   `/usr/local/bin/anime-discovery-admin`.
7. **Units:** install `akon-*` (RN-1) with the new user, paths, `RequiresMountsFor` the same three
   mounts, the admin socket at `/run/akon/admin.sock` and `ReadWritePaths` for `/var/lib/akon` and
   `/etc/akon`.

Each step is checked after it runs. A step that fails stops the rename and prints how to undo the
steps already done (a written undo list, not guesses). Tests: a temporary root with the production
layout above (fake `usermod`/`groupmod`/`systemctl` through the installer's existing `execute`
injection), covering a full run, a dry run, and a failure in the middle.

### RN-3. qBittorrent: the new tag and category; old folders stay (P1)

The owner's answer is RN-0 question 1. Only `src/anime/torrent_client/qbittorrent.py` may call
qBittorrent's API; use its version adapter for the 4.x and 5.x differences.

- **New torrents:** the folder and tag prefix become `akon-` (`series_job/torrents.py`
  `options_for`, the client's `label_fields` in `torrent_client/contract.py`, and every
  `endswith('/ad-'+marker)` check). Each torrent entry in the series row records its folder prefix
  (`qb.prefix`: `"akon"`, or `"ad"` for a torrent added before the rename), so the steps build the
  right folder.
- **Ownership:** a torrent is AKON's when it has the tag `akon-<marker>`, the category from the
  settings, and its save path is `<download root>/<qb.prefix>-<marker>`
  (`metadata_service.verify_torrent_ownership`, `delete_owned`, `remove_series_torrent`,
  `_series_owned`). No other tag starting with `akon-` or `ad-` may be on it.
- **Existing torrents (the rename step):** for each torrent in category `anime-discovery` with
  exactly one `ad-<marker>` tag, saved in `<download root>/ad-<marker>`:
  1. add the tag `akon-<marker>` (`torrents/addTags`) and remove `ad-<marker>` (`torrents/removeTags`);
  2. move it to category `akon` (`torrents/createCategory` first, with no save path change, then
     `torrents/setCategory`).

  Never move files (`setLocation`). Leave every other torrent alone, and print each one changed.
  Then set `qb.prefix = "ad"` on the matching torrents in the series rows (the cutover already gave
  them their markers).
- **The setting:** change `qbittorrent.category` to `akon` in the same run, with the services
  stopped, because ownership checks compare the torrent's category with the setting. Then remove
  the empty `anime-discovery` category.
- **Tests (with a fake transport; see `tests/test_torrent_client.py`):**
  - an old torrent is retagged and recategorized in place;
  - a torrent without an `ad-` tag, or with a different save path, is left alone;
  - a retagged old torrent is still AKON's in the download, drop and Keep cleanup steps;
  - a new torrent is added as `akon-<marker>`.

### RN-4. The web address: `/akon/`, and `/anime-discovery/` still works (P2)

- `src/anime/api/app.py` already accepts both mounts (`for mount in ('/akon','/anime-discovery')`).
  Add a test that the dashboard, `/api/v1/check` and `/api/v1/webhook` answer under both.
- Write the nginx blocks the owner needs into the PR description: `/akon/` (dashboard and API) plus
  `= /akon/api/v1/webhook` and `= /akon/api/v1/check`, copied from the existing `/anime-discovery/`
  ones. The owner applies them; don't edit nginx.
- After the owner points Autobrr at `/akon/api/v1/…`, the old address can be retired (RN-0 question 3).

### RN-5. Old names left in the code (P2)

- `src/transcode_agent/__init__.py` reads `.anime-discovery-release.json`; read `.akon-release.json` first.
- Remove `deploy/nextgen/anime-discovery-*.service` (the installer writes the units) and the old
  `deploy/install-homelab.py` if nothing uses it (check with the owner).
- `ANIME_DISCOVERY_CONFIG_DIR`, `ANIME_DISCOVERY_ADMIN_SOCKET` and `.anime-discovery-release.json`
  stay accepted as fallbacks for one release, with a comment saying when they go.
- Fix `tests/test_adm_settings_folder.py`: it still expects `/etc/anime-discovery` as the default.
- `grep -rn "anime-discovery\|anime_discovery\|ANIME_DISCOVERY\|AnimeDiscovery" src bin deploy` should
  list only those fallbacks and the historical migration files.

### RN-6. Rehearse (P1)

- Run the whole rename (RN-1 to RN-3, with qBittorrent faked) in a temporary root built to match the
  production layout table, including a copy of the real `installer.json` shape and unit files (copy
  their text from production with read-only `cat`).
- Check after it: `systemctl` would list only `akon-*`; `adm` starts with `AKON_CONFIG_DIR=/etc/akon/branches/develop`;
  the settings' `storage.database_path` is `/var/lib/akon/akon.sqlite`; the database opens and is
  unchanged (row counts); `--rollback` restores the old names.
- Write the runbook into the PR description (below), with the exact commands.

## The cutover runbook (for the owner; items 2 and 3 together)

1. Pause Autobrr's AKON Check, or pick a quiet time.
2. Stop `anime-discovery.target`.
3. Back up the database. Run the cutover (item 2): `--apply` converts work and closes the old jobs.
4. Run the rename (RN-2 and RN-3).
5. Install the new release under `/opt/akon` (`akon-install --upgrade --revision <sha>`). This applies
   migration 080 and starts `akon.target`.
6. Check the log, the Running page and `adm running`.
7. Update nginx (RN-4) and point Autobrr at `/akon/api/v1/…`. Resume AKON Check.

## Done when

- RN-1 to RN-3 are implemented, with tests, and the RN-6 rehearsal passes in a temporary root.
- The RN-0 answers are written into this file.
- The runbook in PR 349 has the exact commands.
