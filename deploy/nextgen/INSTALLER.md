# Anime Discovery installation and upgrades

Run the standalone `bin/anime-discovery-install` from a reviewed checkout. It uses Python's standard library, asks questions by default, and accepts switches for unattended operation. Python 3.11+, git, runuser, systemd and Python venv/pip are required. Use `--install-system-deps` to install dependencies with apt-get on Debian/Ubuntu; this is never implied by `--yes`.

Default configuration files (`settings.json`, `secrets.json`, and the administrator password record) live in `/etc/anime-discovery`, owned by the service account with directory mode 0700. The database and runtime state live under `/var/lib/anime-discovery`; application releases live under `/opt/anime-discovery`. Existing installation paths and configured database filenames are preserved on upgrades.

Install interactively:

```sh
sudo python3 bin/anime-discovery-install
```

Install unattended from a local reviewed Git checkout:

```sh
sudo python3 bin/anime-discovery-install --yes --source-dir /path/to/checkout --public-origin https://anime.example.com --settings-file /path/to/native-settings.json --start api --enable
```

`--source-dir` installs committed tracked files at HEAD, not working-tree edits. Otherwise the installer fetches the private repository's configured branch using root's existing Git access. `--revision FULL_COMMIT_SHA` pins a release. It creates a virtual environment per release.

## Installed versions

Each install or upgrade is a folder named after its date, branch and commit, and `current` points at the running one:

```
/opt/anime-discovery/
  current -> releases/2026-11-06-NextGen-c143bbb9
  releases/
    2026-11-06-NextGen-c143bbb9/   running
    2026-11-02-NextGen-9071be2e/   the previous version, kept for rollback
```

A `--source-dir` install is labelled `local`. Installing the same commit twice adds `-2`, `-3`. The new version is built completely (source and virtual environment) before the services stop, then `current` is switched in one step; a virtual environment cannot be moved, which is why each version keeps its own folder.

After a successful install or upgrade, only the running version and the one before it are kept. Older versions, leftovers of interrupted runs and their unit backups are deleted. A failed upgrade deletes nothing.

Each database upgrade first saves a copy beside the database (`anime-discovery.sqlite.before-vN-<id>.sqlite`). After a successful database upgrade only the two newest copies are kept.

## Rollback

```sh
sudo anime-discovery-install --rollback
```

Switches `current` back to the previous version, restores its unit files and restarts the services that were running. First the previous version checks the database and settings itself (`python -m src.anime check`). If an upgrade moved them beyond what that version understands, the rollback refuses and says why; nothing is changed. Nothing is deleted: the newer version becomes the previous one, and the next upgrade replaces it.

## The installer updates itself

An upgrade installs the new release's own installer as `/usr/local/bin/anime-discovery-install`. When the fetched release brings a different installer, the upgrade continues with that new installer on the same checkout, once, so installer changes apply on the upgrade that delivers them.

The installer writes `/usr/local/bin/anime-discovery-install`. Its root-owned private metadata directory is `/var/lib/anime-discovery/installer`: `installer.json` stores installation settings, `installation.json` records the last successful installation, and `installation.pending.json` records interrupted setup. The lock and new unit backups also live there. A successful upgrade migrates older JSON metadata from the application folder; failed upgrades retain it for recovery. Custom paths use a small locator beside the installed command. The working directory does not select the installation.

Upgrade with:

```sh
sudo anime-discovery-install --upgrade --yes
```

Running the command without `--upgrade` or `--rollback` installs. There is no `--install` switch, and switches must be spelled out in full (no abbreviations).

To switch the installation to another branch:

```sh
sudo anime-discovery-install --upgrade --branch refactor
```

The branch is fetched and installed, its release folder is named after it (`…-refactor-<commit>`), and later upgrades keep using it until you pass `--branch` again. It is the only installation setting an upgrade may change. Every upgrade, with or without `--branch`, rewrites the systemd unit files from the new release (backing up the old ones) and reloads systemd.

### Each branch keeps its own settings and database

Branches can carry different database migrations and settings versions, so a branch never runs on another branch's data:

| | Settings, credentials, password | Database |
|---|---|---|
| The branch you installed first | the configuration folder you installed with (`/etc/anime-discovery`) | the database in its settings |
| Any other branch | `/etc/anime-discovery/branches/<branch>/` | `/var/lib/anime-discovery/anime-discovery-<branch>.sqlite` |


`<branch>` is the branch name with `/` replaced by `-`. The installer records which folder belongs to which branch in `/var/lib/anime-discovery/installer/installer.json`.

- **The first switch to a branch** sets up its folder:
  - the credentials and administrator password are copied;
  - the settings are copied and converted by the new branch's own settings upgrade, with the database path pointing at the branch's file. If the new branch cannot read them (they are newer than it understands), the switch is refused and nothing changes.
  - The new branch starts with an **empty database**, unless you ask for a copy:

    ```sh
    sudo anime-discovery-install --upgrade --branch refactor --copy-db-data
    ```

- **Switching back** to a branch you used before reuses its own folder and database exactly as you left them, so changes made on one branch never reach the other. That includes settings and passwords.
- **`--rollback`** to a release of another branch uses that branch's folder too.
- **A failed first switch** removes the new branch's folder and database again, and the current branch is untouched. Leftovers from an interrupted switch are never reused: the installer refuses until you move them away.


### Which settings folder `adm` uses

`adm` (report, recover, services check) and the `fixes/` scripts use the
settings folder of the branch that is **running**: the `--config-dir` of the
installed worker unit (for example `/etc/anime-discovery/branches/refactor`).
`ANIME_DISCOVERY_CONFIG_DIR` overrides it, and an explicit `--config-dir` always
wins. Without an installed unit they use `/etc/anime-discovery`.

### When a database can be copied

`--copy-db-data` copies the current branch's database (an SQLite backup, taken while the services are stopped). The new branch then upgrades the copy with its own migrations. Before copying, the installer checks that the two branches' **migration files are identical, byte for byte, up to the database's version**. A version number alone proves nothing: two branches can both reach v65 with different tables, or with the same tables but different data rewrites. Identical migration files mean the new branch would have built exactly this database.

If the histories differ, the switch stops before anything is copied, and the message names the first migration that differs. The new branch's own upgrade then also checks the copy's structure, foreign keys and integrity before using it. A database newer than the new branch understands is never copied.

For example, NextGen and `refactor` share migrations 001–061. A NextGen database at v61 can therefore be copied into `refactor`, which then upgrades the copy to v65. A `refactor` database at v65 can never be copied back into NextGen, and switching back simply returns to NextGen's own database.

`--copy-db-data` needs an installer that knows it: on an older installation, run `--upgrade` once first, so the current branch's installer is updated.

Existing native settings, credentials, administrator password, and configured database path are preserved. Native configuration and database upgrades run explicitly with the services stopped, using the maintenance preview/backup contracts. Legacy configuration and databases are not imported. Use a separate fresh Anime Discovery directory.

Fresh installations start only the API. The API and worker are separate Type=notify services with 60-second event-loop watchdogs. The worker waits for API readiness and stops when the API stops; worker failure leaves the API running. Restart the worker explicitly after API recovery. Units include configured media mount dependencies and write paths; the installer does not create, move or recursively change ownership of media directories.

Set the administrator password locally after API startup:

```sh
sudo /opt/anime-discovery/current/bin/adm set-password
```

The API itself serves the restricted `/run/anime-discovery/admin.sock`. ADM uses that socket; no management bearer token or separate admin daemon is created. For unattended first-time setup, supply a protected `--admin-password-file` with `--start api` or `--start all`. The installer passes it through stdin to ADM with `--if-unset`, so upgrades preserve an existing password. The separate optional `--webhook-key-file` seeds only an absent webhook key. Credentials are never printed or placed in command arguments.

The dashboard uses password sessions and same-origin `/api/v1` requests. Configure the exact public origin and proxy both dashboard pages and the API to the loopback listener. Configure providers in the dashboard, run worker readiness, then start/enable the worker:

```sh
sudo -u anime-discovery /opt/anime-discovery/current/.venv/bin/python -m src.anime check --config-dir /etc/anime-discovery --worker
sudo systemctl enable --now anime-discovery-worker.service
```

`--start preserve` is the default and retains running/stopped choices on upgrade. `--start none`, `api`, or `all` overrides it. Starting all requires worker readiness. `--enable` enables the selected component services for boot; existing enablement is retained.

If setup or activation fails, Anime Discovery remains stopped and `installation.pending.json` identifies the candidate and previous release. Existing units are backed up under `unit-backups`. Do not point old code at a database whose schema has advanced. Correct the cause and rerun; rollback is never automatic (see Rollback).

No live installation has been performed by the implementation tests. Follow `src/anime/SERVICE_SETUP.md` for host validation: password setup/login, verified webhook intake appearing in Recognized Names, worker crash isolation, API recovery followed by worker restart, mount access, and provider connectivity.

The installer prints a readable completion summary and service instructions by default. Use `--json --yes` for automation; progress messages go to stderr and the JSON result goes to stdout. 

From `/opt/anime-discovery`, run `sudo ./current/bin/adm set-password`; `current` follows the active release. The executable `bin/adm` is also usable directly from the checkout. The installer installs its launcher beside the installer command (by default `/usr/local/bin/adm`), pointing at the current release. Run it as root or the service account; the API must be running.
