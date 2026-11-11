# AniKeepOrNot

Private development repository for the AniKeepOrNot homelab application.

## Status
**Homelab dashboard, installer, Autobrr intake and acquisition daemon; VM installation not yet verified.**

GitHub commits do not automatically deploy to the AniKeepOrNot VM.

## Scope
Autobrr webhook ingestion, canonical anime identity, controller and episode tracking, qBittorrent verification and acquisition, dashboard and authentication, safe staging, rejection enforcement, and administrative CLI.

## Live homelab runtime (configuration only)

All three entrypoints read `/etc/anime-discovery/settings.json` and
`/etc/anime-discovery/secrets.json`. There are no runtime flags or
environment-variable credentials.

Example `settings.json`:

```json
{
  "daemon": {"database_path": "/var/lib/anime-discovery/anime-discovery.db", "concurrency": 4, "lease_seconds": 300},
  "web": {"host": "0.0.0.0", "port": 8765},
  "storage": {"download_root": "/path/to/qb-downloads", "trial_root": "/path/to/anime-trial"},
  "qbittorrent": {"base_url": "http://127.0.0.1:8080", "username": "admin", "cleanup_source": true},
  "identity": {"series_map": {"Example Show": {"tvdb_id": 123, "anidb_id": 456, "season": 1}}}
}
```

Example `secrets.json`:

```json
{"web_token": "choose-a-webhook-token", "admin_password": "choose-a-different-dashboard-password", "qbittorrent_password": "your-qb-password"}
```

**First deployment (new VM code, existing JSON configuration is supported):**

Clone the private repository onto the VM as the intended service account:

```bash
git clone --branch develop git@github.com:bugz8unny69/anime-discovery.git /opt/anime-discovery
cd /opt/anime-discovery
sudo python3 deploy/install-homelab.py
```

The interactive installer detects whether
`/etc/anime-discovery/settings.json` **and**
`/etc/anime-discovery/secrets.json` already exist.

- With **existing JSON files**, the installer previews missing settings,
  asks permission to merge the defaults with a backup, and **never replaces
  existing values or rewrites secrets**. It validates configured storage,
  credentials and ownership before creating a brand-new database at the
  `daemon.database_path` value already in settings.json. If an abandoned
  database exists there, remove or archive it **yourself** before installing;
  the installer refuses to overwrite it.
- With **no JSON files**, the installer prompts for storage and qBittorrent
  settings, writes new JSON files, initializes a fresh database and installs
  the systemd service. A partial configuration is refused.

The service account must own the existing mode-0600 `secrets.json` and
have access to the configuration, media and database paths. The installer
refuses to change ownership of existing secret files silently.
A pre-existing systemd service is also refused, to avoid disrupting another
deployment. For subsequent Git upgrades, install and use the independent
`/usr/local/sbin/anime-discovery-upgrade` after its PR is reviewed and merged.


The launcher starts the dashboard/webhook and acquisition worker together.
Open `http://HOMELAB-IP:8765/` with username `anime` and the separate
`admin_password`. Autobrr POSTs to `/webhook` using
`Authorization: Bearer YOUR_WEB_TOKEN` and fields `release_name`,
`source_guid`, `info_hash`. The dashboard can approve a notification
and queue its acquisition. `qbittorrent.cleanup_source=true` enables
removing the torrent and downloaded files after staging succeeds.

### Autobrr automatic handoff

POST to `/webhook` with the bearer token. The service accepts
`release_name` or `torrentName`, plus `info_hash` or `torrentHash`.
The source GUID and magnet URI are optional; the torrent hash is used
as the GUID fallback and a magnet is generated from it.

Episodes enqueue automatically with explicit TVDB/AniDB and season/episode
values **or** via the dashboard's **Recognized anime names** mappings.
Configured aliases recognize `S01E02` and `[Group] Example Show - 02`.
Unmatched releases remain in the inbox for manual approval.

Series inspection displays dispatch, job and staged media records.
Reject blocks work, Clear History hides earlier recognized inbox events
without lifting rejection, and Reset lifts the block for new webhooks.
If configured, successful staging/finalization removes both the torrent and
its downloaded source files from qBittorrent.

## Migrating existing VM configuration

If `/etc/anime-discovery/settings.json` and `secrets.json` already exist,
**use the installer’s existing-configuration adoption path**; it preserves both files.
The new admin command merges missing supported defaults into the existing
`settings.json`, preserving the current database path, download/trial paths,
qBittorrent URL, user preferences, and credentials.

Review the proposed additions first:

```bash
sudo python3 /opt/anime-discovery/src/legacy/anime-discovery-admin.py settings migrate
```

Then explicitly apply the changes:

```bash
sudo python3 /opt/anime-discovery/src/legacy/anime-discovery-admin.py settings migrate --apply
```

The command creates a dated `settings.json.before-migrate-*.bak` backup
containing the **exact original bytes** before replacing `settings.json`.
It never rewrites `secrets.json`; Prowlarr remains disabled by default.
Existing values always win over defaults. Unknown legacy keys or incompatible
values stop the migration and require review rather than being erased.
No SQLite records, torrent data, or service configuration are changed by
`settings migrate`.

To enable the *optional* new Prowlarr integration after migrating, configure
`prowlarr.base_url`, add `prowlarr_api_key` to the existing secrets file
without removing any existing keys, and set `prowlarr.enabled` to true.
Keep `secrets.json` readable only by its owner (mode 0600).

## Fresh-build configuration and credential repair

The fresh installer asks for separate dashboard and webhook credentials and
can configure the working optional Prowlarr one-time discovery integration.
Dashboard Basic authentication (user `anime`) uses `admin_password` only.
Autobrr `POST /webhook` uses `web_token` as its Bearer token only;
neither credential authorizes the other route type.

If you already installed the fresh build, **do not delete your new SQLite database**.
After deploying a release containing this change, run:

```bash
sudo python3 /opt/anime-discovery/src/legacy/anime-discovery-admin.py settings configure-credentials
sudo systemctl restart anime-discovery.service
```

Leave the webhook prompt blank to keep your Autobrr token. Enter a distinct
dashboard password when prompted. The command rewrites only `secrets.json`,
preserving existing keys and file ownership. An archive upgrade from the earlier build can temporarily start with the old
shared token so the service does not fail the upgrade health check. This is
a transitional security exception: immediately run configure-credentials
and restart to eliminate shared credentials. New installations always prompt
for separate credentials. To configure Prowlarr on an existing
installation, use `settings set prowlarr.base_url` and
`settings set prowlarr.enabled true`, and provision `prowlarr_api_key`
securely in `secrets.json` (0600), then restart the service.

**Integration status:** Prowlarr search and opt-in Jellyfin immediate library
refresh are implemented. The refresh is requested only after a staged episode
is finalized and is best-effort: a Jellyfin outage never removes media or
retries the acquisition. Jellyfin still indexes the NFO files on its NFS
mount. No Jellyfin API reads or item deletion are performed.
ntfy notifications and TVDB API lookups are not implemented in this fresh
runtime; entering their URLs or keys would not enable those services.
The installer intentionally does not solicit unused credentials.

### Immediate Jellyfin scan on NFS

Fresh installs ask whether to enable immediate Jellyfin scans, and if enabled
collect a URL and API key. A successful episode transfer and NFO write precede
the durable SQLite finalization; only then does the worker send
`POST /Library/Refresh` with a private `X-Emby-Token` header, bounded by an
8-second timeout. Scan failure is logged without leaking server URLs or API
keys and does not fail the acquisition. Jellyfin schedules its scan; indexing
may take additional time. A failure does not automatically retry the scan.

For existing installations, after deploying the new release, run
`settings migrate` to preview new settings and `settings migrate --apply`
to add defaults. Then run the interactive admin command, which securely updates the key\nand enables scanning without showing the key in process arguments:\n\n```bash\nsudo python3 /opt/anime-discovery/src/legacy/anime-discovery-admin.py settings configure-jellyfin\nsudo systemctl restart anime-discovery.service\n```\n\nNo SQLite reset is required.

## Upgrading an existing Git installation

The supported application upgrade path is a standalone Python bootstrapper
installed at `/usr/local/sbin/anime-discovery-upgrade`. It runs independently
of the application runtime, fetches `develop` from the pinned private SSH
repository `git@github.com:bugz8unny69/anime-discovery.git`, and performs
in-place fast-forward Git updates. No Docker or archive inputs.

After the bootstrapper PR has passed CI, been reviewed, **merged**, and cloned
onto the VM, install the independent program once:

```bash
sudo -H python3 /opt/anime-discovery/deploy/anime-discovery-upgrade.py --install
```

Then run future upgrades with:

```bash
sudo -H /usr/local/sbin/anime-discovery-upgrade
```

The root-run command authenticates using root's effective SSH configuration;
the Git checkout is returned to its original `anime-discovery` owner before
service startup. The tool backs up SQLite, preserves `/etc/anime-discovery`,
tracks the last successful Git commit at
`/var/lib/anime-discovery/upgrade-state.json`, and refuses unsafe or
non-fast-forward state. If interrupted, it attempts conservative recovery
when the checkout exactly matches a known commit and is clean. Arbitrary
partial filesystem corruption may still require intervention.

`anime-discovery-admin.py` is for application, settings, and database
administration only. It **does not** expose an `upgrade` subcommand.
The former **local ZIP/tar.gz upgrade implementation has been removed**.
The manual GitHub archive-publishing workflow is a separate historical
release facility; it is not an upgrade input and was not modified by this PR.

### Future enhancement: trusted online archive source (not implemented)

If releases eventually become public, consider fetching a versioned archive
from an approved official HTTPS endpoint (GitHub Releases or a separately
controlled distribution hostname). No URL or hostname is configured yet.
That feature needs explicit opt-in, trusted publisher and checksum/signature
verification, immutable release identifiers, safe extraction, rollback tests,
and a defined policy for SSH-versus-HTTPS use. **Do not treat an arbitrary
URL, manually supplied local archive, or HTTPS fallback as supported today.**

For a fresh VM installation, refer to the existing
`deploy/install-homelab.py` instructions above. **Never rerun the fresh
installer against an installed service or an existing database.**

## Built-in VM administration

```text
anime-discovery-admin.py status
anime-discovery-admin.py database init
anime-discovery-admin.py database doctor
anime-discovery-admin.py settings show
anime-discovery-admin.py settings set KEY VALUE
```

Existing `doctor` and `new-database` spellings still work for older scripts.
The application reads settings and secrets from flat files; no environment
variables or worker flags are required.

## Dashboard: Series and Jobs

The dashboard has separate tabs for **Series**, **Jobs**, and
**Recognized names**.

**Series** is the Anime-Trial library: an anime appears there only after
an acquisition has completed and at least one staged episode remains on disk.
Simply receiving a webhook or recognizing the anime does not add it to this
view. Series rows show staged episode count, last staged episode and
management status. Select any visible rows with checkboxes, then apply
Reject, Clear History or Reset as one database transaction. List pages
support 20, 50 or 100 rows; selection is confined to the current page.

**Jobs** shows all received Autobrr releases, including unmatched releases,
queued acquisitions and completed episodes. States include Received,
Queued, Running, Waiting for Download, Completed, Blocked and Needs Review.
The open Jobs tab refreshes automatically every five seconds, showing
new notifications and status changes without a manual page reload.
Manual episode approval for unmatched jobs remains accessible here.

**Recognized names** holds the configured TVDB/AniDB title aliases
used to match incoming releases, whether or not they have reached
Anime-Trial. This prevents the library from being confused with the
inbox or the identity configuration.

### Jobs: cancel and cleanup (operator review)

Cancel is available only for an idle acquisition job in **Needs Review**.
It archives that acquisition attempt and keeps its original audit history;
it does **not** stop a leased worker or immediately delete anything.
An interrupted acquisition with verified staged media must be recovered,
not canceled.

Canceled jobs with durable torrent provenance show **Review cleanup**.
The preview checks current qBittorrent category/tag ownership, other jobs
and notifications that might use the same torrent, and the verified download
folder before enabling any destructive action. Two choices are separate:

- **REMOVE TORRENT ONLY** removes proven-owned torrent entries while
  preserving download files and all staged media.
- **REMOVE OWNED DOWNLOAD FILES** is an explicit stronger opt-in available
  only when qBittorrent is stopped and every source file, configured
  storage root, Sonarr overlap, and competing qBittorrent file path pass
  preflight. No partial/unknown, symlinked, hardlinked, shared or
  out-of-root files are intentionally removed. Never select this just to
  bypass manual verification.

A failed or interrupted cleanup persists **Cleanup needed** and exposes
**Retry cleanup**. qBittorrent timeouts are reconciled against the exact hash
before any new removal attempt. If torrent metadata disappears but downloaded
files remain, the system preserves those files for manual review rather than
blindly deleting them. **Canceled** never implies all files were cleaned.
This operation is not performed automatically by the background worker.
Keep a separate verified backup before deploying schema-changing updates.

## Shared Prowlarr episode discovery

Add this section to `/etc/anime-discovery/settings.json`:

```json
"prowlarr": {
  "enabled": true,
  "base_url": "http://YOUR-PROWLARR-HOST:9696"
}
```

Add `"prowlarr_api_key": "YOUR_KEY"` to
`/etc/anime-discovery/secrets.json` (file mode 0600) and restart the service.
Do not put the API key into settings.json, environment variables or flags.

Incoming **Autobrr** episode notifications still enqueue their own verified
release immediately. They also trigger a bounded, one-time Prowlarr search
for earlier episodes, skipping items already acquired or queued.

The **Request Anime** action on the Series page launches a manual **one-time**
Prowlarr search. Enter a known anime name or TVDB/AniDB ID, optionally select
an anime season, and submit. No episode selection and no monitoring of future
releases. The name or ID must already have an unambiguous mapping under
Recognized names. Manual and Autobrr search results pass into the same
verified acquisition and qBittorrent/Anime-Trial staging workflow and
appear in the Jobs page.

Current Prowlarr integration processes magnet-backed, explicitly numbered
episode results with matching title aliases. Torrent download URLs and
anime-title/provider lookup without a recognized mapping are not supported.
Ambiguous releases are skipped rather than guessed.

## Offline fresh-install smoke check

From the repository root with Python 3.12:

```bash
cd src/legacy
python3 anime-discovery-admin.py --db /tmp/anime-discovery-smoke.db new-database
python3 anime-discovery-admin.py --db /tmp/anime-discovery-smoke.db doctor
python3 -m unittest -v test_anime_fresh_acquisition_integration test_anime_webhook_lifecycle test_anime_web_http test_anime_live_acquisition
```

Choose a new database path; `new-database` intentionally refuses to overwrite an
existing file. `doctor` is read-only and exits nonzero for an incomplete database.
These commands do not start services, enable acquisition, or contact qBittorrent.
They are a database smoke check, **not** proof that the complete dashboard,
webhook server, installer, or deployment is ready.

## Filesystem layout
Application: `/opt/anime-discovery`; configuration: `/etc/anime-discovery`; state: `/var/lib/anime-discovery`. The installer installs the systemd unit on the homelab host.

See [PROJECT_STATUS.md](PROJECT_STATUS.md) for progress and [SECURITY.md](SECURITY.md) for repository hygiene.

The web service and acquisition daemon are in `src/legacy`; migration-era components may still contain functionality not connected to this live application.
