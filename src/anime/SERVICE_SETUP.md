# Anime Discovery setup and first lab run

The source now includes an explicit local setup/maintenance CLI, a composed worker
entry point and systemd examples under `deploy/nextgen/`. They have not been
installed or run here. Configuration is v10, secrets support a fresh v7 schema (older schemas readable) and SQLite
uses v26. Recognized Names saves verified webhook release aliases and exact episode placements automatically in the verification transaction. Multiple AniDB works may share a TVDB season. Shared aliases remain ambiguous and fall through to normal identity resolution. Episode details are scoped to the selected AniDB work. Existing Anime Discovery databases require the explicit maintenance upgrade; v26 preserves v25 recognition evidence. This does not import data from the older application.
No service starts, schema upgrades or media operations happen on Python import.

For the interactive or unattended installer and native upgrades, see [INSTALLER.md](../../deploy/nextgen/INSTALLER.md).

## Prepare the host

Use Python 3.11 or later, a virtual environment, `/usr/bin/ffprobe`, Linux/Unix
file locking and a dedicated non-root account named `anime-discovery`. Install
the reviewed source at `/opt/anime-discovery` and its virtual environment at
`/opt/anime-discovery/.venv`. Install `requirements-nextgen-api.txt` into that
virtual environment. Keep source and the virtual environment read-only to the
service account. The worker's existing SIGTERM/SIGINT path stops new claims and
drains current attempts; the systemd units wait for that drain instead of imposing
a stop deadline. The units use `Restart=on-failure`; intentional stops stay stopped.

Provision these directories, owned/writable by the service account:

| Path | Purpose |
| --- | --- |
| `/var/lib/anime-discovery` | Local SQLite/WAL, lock files and maintenance backups |
| `/etc/anime-discovery` | Private settings and credentials; directory mode 0700 |
| `/srv/anime-discovery/downloads` | Shared qBittorrent payload root |
| `/srv/anime-discovery/staging` | Review media and generated NFOs |
| `/srv/anime-discovery/library` | Permanent media managed by Sonarr |

SQLite and its locks/backups must be on local storage. Media may be on NFS, with
correct UID/GID permissions for this account and the media services. The same
underlying download files must be accessible to qBittorrent and the worker at
the recorded download path. Sonarr's staging/library path settings can map its
container paths to the same underlying folders. Do not mount SQLite on NFS.
Use dedicated, disjoint roots; do not recursively change permissions on an
existing library. The worker requires non-root operation for staging.

If using other paths, edit the settings and both `RequiresMountsFor` and
`ReadWritePaths` in the worker unit. Missing media mounts must be fixed before
starting the worker. Unit examples do not create or mount media shares.

## Fresh installation

Make a copy of `deploy/nextgen/settings.example.json`, fill the registered AniDB
client name/version and the service addresses you already know. This is a plain
settings object, not a versioned settings envelope; never put API keys in it.
Run from `/opt/anime-discovery` as the service account:

```sh
.venv/bin/python -m src.anime init --config-dir /etc/anime-discovery --database /var/lib/anime-discovery/anime-discovery.sqlite --settings-file /path/to/settings.json

```

The configuration library creates fresh local configuration and storage; this does not import any earlier application's data. Start the API service before setting the dashboard password, then run the local administrator CLI under the service account:

```sh
systemctl start anime-discovery-api.service
sudo -u anime-discovery /opt/anime-discovery/.venv/bin/python -m src.anime.api.adm set-password
```

The administrator password is never copied from earlier credentials. Sign in to the dashboard and set the independent Autobrr webhook token under Settings > Private credentials. Alternatively, `python -m src.anime.api init-access --config-dir ...` can provision the webhook token first; this command does not set an administrator password.

Edit the API unit's placeholder origin to the exact dashboard HTTPS origin.
Copy the three example units into `/etc/systemd/system` and reload the systemd
unit definitions. Do not install the `.invalid` placeholder as a working origin.
The API binds to loopback at port 8765 by default. Forward the HTTPS origin to
that loopback listener with the original Host and Authorization headers. Disable
proxy request logging for credential-bearing webhook URLs; prefer a separate
Bearer webhook token. The API does not trust forwarded headers or open a public
listener. Set API host/port only through settings and restart the API after edits.

The API and worker units use `Type=notify` with a 60-second watchdog. The API reports readiness after Uvicorn binds its listener; the worker reports readiness after its first recovery pass. Each sends watchdog heartbeats from its asyncio event loop. This detects a blocked event loop, but does not certify provider connectivity or the completion of individual long-running jobs. The API unit can be started without the worker; the worker binds to and starts after the API, so an API stop also stops the worker. Worker failure does not stop the API. Start or restart the worker explicitly after API recovery. Use `systemctl status` and `journalctl` to inspect watchdog failures and restart throttling.

Start only `anime-discovery-api.service` initially. Open the dashboard and sign
in with the administrator password. The webhook key cannot manage settings or jobs.
The dashboard uses session/CSRF protection and `/api/v1/` resources.

Configure settings and private credentials through the dashboard:

- AniDB registered client name/version and the required TVDB API key.
- A trusted torrent metadata base folder serving `<lowercase-infohash>.torrent`.
  Arbitrary URLs from webhook deliveries are not used as metadata sources.
- qBittorrent address, category, private username/password and shared root.
- Local staging and permanent roots; Sonarr address and quality profile ID
  for adding new series. Existing series can use profile ID zero.
- Sonarr-visible staging/library paths when its mount layout differs.
- Optional Jellyfin address/API key, with staging and permanent folders configured
  as intended in Jellyfin. Scan acceptance does not establish episode visibility.
- Optional Shoko address/API key for AniDB metadata lookup only. Blank disables it.
- The configured episode window and size limits. There is no automatic search
  for missing episodes; later verified release deliveries enter through intake.

Run local readiness before starting the worker:

```sh
.venv/bin/python -m src.anime check --config-dir /etc/anime-discovery --worker
```

This checks local configuration, schema/integrity, credential presence, non-root
worker use and ffprobe availability. It does not contact external services or
claim a job. `local_ready` is not an end-to-end health guarantee. The authenticated
`GET /api/v1/health` endpoint checks API/local storage readiness; it does not prove
that the worker is running or providers can be reached.

For the permitted first lab run, start `anime-discovery-worker.service`. Once
configured, `anime-discovery.target` starts/stops both services together and may
be enabled for boot. API and worker are separate processes with the same config
directory/database. Settings marked restart-required need the indicated process
restart; the units do not invent an IPC reload mechanism. Use journald for both
units' readable diagnostics; there is no additional log-file rotation setup.

## Upgrading an existing Anime Discovery lab

Stop `anime-discovery.target` and both component units, then install the matching
reviewed source/virtual environment. Do not run old and new versions together.
Use the explicit configuration preview, then the returned source fingerprint:

```sh
.venv/bin/python -m src.anime config-preview --config-dir /etc/anime-discovery
.venv/bin/python -m src.anime config-upgrade --config-dir /etc/anime-discovery --fingerprint RETURNED_SOURCE_FINGERPRINT --services-stopped
.venv/bin/python -m src.anime database-preview --config-dir /etc/anime-discovery
.venv/bin/python -m src.anime database-upgrade --config-dir /etc/anime-discovery --plan-token RETURNED_PLAN_TOKEN --services-stopped
```

The assertions mean both services are stopped and this source is the version you
intend to run. Configuration upgrade preserves a backup; database apply uses
existing backup and exclusive maintenance contracts. The plan token pins source
schema and migration scripts. A changed plan or failed upgrade stops the operation;
a failure after requesting maintenance leaves admission closed. No automatic
startup migration is performed. Never substitute `init` for an upgrade.

A later database preview can run under the retained maintenance marker without
reopening admission. Fix the failure with services stopped, preview again and
apply the new plan token with the same installed source. A successful current-
version apply validates schema/configuration and explicitly reopens admission.
Do not delete maintenance markers by hand or edit published SQL scripts.
Secrets v1–v5 remain readable; setting a Shoko key explicitly upgrades that file
to v6 and preserves other credentials. Restart only after local checks succeed.
Backups contain private paths/data and belong with the private local state.

## Lab validation still to perform

Use disposable media and a dedicated test torrent/library. These checks have not
been executed in this work:

1. Validate the fresh install and v22→v23 database upgrade/rollback fixtures.
2. Deliver one verified webhook and follow Intake → verification → selected
   download → Staging; confirm the video move and NFOs, then Jellyfin visibility.
3. Keep one episode. Confirm exact TVDB Sonarr placement, no automatic search,
   successful permanent playback, selected staging cleanup and owned download
   cleanup. Confirm shared metadata and kept duplicate protection survive.
4. Reject another episode. Confirm selected staged/download cleanup and that a
   later delivery for the rejected AniDB anime is held before acquisition.
5. Cancel unfinished acquisition. Confirm drain, owned-resource cleanup and
   reservation release; later verified intake should receive fresh claims.
6. Interrupt/restart around add/import/stop/remove boundaries. Confirm recovery
   never repeats an uncertain mutation, and review/retry messages remain accurate.
7. Disable or disconnect Shoko/Jellyfin. Keep and cleanup must remain valid.
   Confirm optional AniDB metadata and refresh jobs report their own outcomes.
8. Stop/restart the target, check mount ordering, process drain and boot behavior;
   verify separate management/webhook credentials and browser CSRF in the lab.

Uncertain client commands that remain present are deliberately not reissued.
Review the recorded job history and client state; do not reset journals to grant
a second mutation. These cases require operator investigation during validation.
