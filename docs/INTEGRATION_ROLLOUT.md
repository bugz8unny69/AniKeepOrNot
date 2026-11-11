# Integration rollout checkpoint (development only)

This branch is **not approved for VM deployment**. It retains the current `develop` runtime and adds a draft Keep → Sonarr handoff for isolated testing. No installed database or source files are altered by the GitHub commits.

## Configuration

Use `settings migrate` to preview newly supported JSON sections before explicitly applying. Never paste production secrets into GitHub.

- `sonarr.enabled` (default false), `base_url`, `root_folder`, `quality_profile_id`; place `sonarr_api_key` in mode-0600 `secrets.json`.
- `tvdb.enabled` (default false), `base_url` (defaults to the TVDB v4 endpoint); place `tvdb_api_key` in `secrets.json`. When enabled, Keep verifies its TVDB ID read-only before Sonarr admission.
- `ntfy.enabled` (default false), HTTPS `server`, `topic`, HTTPS `dashboard_url`, `reminder_hours`; optional `ntfy_token` in `secrets.json`. Newly received unmatched webhooks produce best-effort notifications; recurring reminders are not yet connected to the unified database.
- `shoko.enabled` (default false), `base_url`, `auto_link_verified`, `poll_seconds`; optional `shoko_api_key` is supported by existing settings. No Shoko episode link writes should be enabled until a pinned API contract and exact file/episode identity are verified.
- `jellyfin.enabled` (default false), `base_url`; `jellyfin_api_key` remains in `secrets.json`.

## Keep semantics in this branch

A Keep click requires verified trial media and a TVDB ID. It verifies the configured Sonarr Anime profile and `/mnt/media/Anime` root using the live Sonarr API, then adds or finds the series by TVDB ID. It requests Sonarr ManualImport with `importMode=copy` after verifying each exact staged file maps to one Sonarr episode. The resulting command ID and import manifest are persisted as `SONARR_IMPORT_PENDING`.

A separate monitor polls the command. Once completed, it verifies that Sonarr's imported episode files exist under the configured root and have identical SHA256 hashes to staged media. It then checks that no other release uses the same torrent hash and, if the original torrent still exists, its save path lies in the configured download root, not the trial or Sonarr root. Before `deleteFiles=true`, every video file inside the torrent must hash-match a staged imported item; this blocks partial multi-episode pack deletion. Only after those checks may the original torrent and trial media be deleted and the series marked `KEPT`.

The original torrent is **not** automatically reassigned to Sonarr by adding a series. Deleting an old torrent is distinct from Sonarr managing future acquisitions through its own download-client setup. Existing qB cleanup after staging may mean the old torrent is already absent; that is handled as an idempotent no-op. API/schema behavior and network mount permissions are still subject to VM acceptance.

Shoko file/episode linkage remains read-only and fail-closed until the 5.3.3.0 API contract is pinned and tested against the internal Swagger specification.

## Acceptance gates

1. Mock-client and API tests pass on the feature branch, then review/merge the PR into `develop`.
2. Confirm the deployed configuration and API permissions on each host without printing secret values.
3. Simulate duplicate, unmatched, rejected, and approved webhooks; verify notification behavior.
4. Verify NFS trial file visibility, Jellyfin refresh, Sonarr creation on Keep, and Shoko's passive metadata behavior.
5. Only after integrated results are accepted, produce a new archive from `develop`. Do not merge into `main` or deploy as part of this draft.

## Confirmed homelab integration targets (2026-10-19)

- Sonarr hostname `sonarr.arr.internal`; configure an explicit HTTP(S) URL with an appropriate, verifiable certificate.
- Sonarr root folder as seen by Sonarr: `/mnt/media/Anime`.
- Sonarr quality profile display name: `Anime`; numeric profile ID must be discovered from the server before enabling.
- Shoko Server: 5.3.3.0; internal Swagger UI at `https://shoko.arr.internal/swagger/`. API schema inaccessible from this execution environment; no Shoko write action approved.

**Requested KEEP policy:** Import existing staged trial episodes into Sonarr's permanent root. User also wants original trial files and the original AniKeepOrNot torrent payload removed. **This is now implemented behind the confirmation monitor, but has not been tested against the real VM.** The monitor deletes original data only after Sonarr file and torrent payload verification. Before cleanup, independently verify actual Sonarr episode-file records, on-disk destination files, and torrent ownership. Adding a series in Sonarr does not automatically transfer management of an existing qBittorrent torrent. Never delete that torrent on the assumption Sonarr adopted it; coordinate the torrent client and ownership handoff explicitly. Leave NFS data unchanged on timeout, unclear ownership, ambiguous episode mapping, or partial import.

**Important:** Sonarr's root folder and trial folder must be distinct. Confirm the exact trial root and ensure both services resolve the same staged file path for Manual Import. This is a deployment prerequisite, not an assumed path mapping.


## Shoko 5.3.3.0 Swagger contract obtained

User-supplied live exported OpenAPI document: 1,029,653 bytes; SHA256 `62c01fb090c77e8ec9340f1768da1fa0f100dbe1fdf24aa0a1c5e0ea9726be80`. Document declares `Shoko API 3.0`, base server path `/api/v3/`, and `apikey` HTTP header authentication; API schema version is not itself the Shoko server version.

Exact documented read contracts:
- `GET /api/v3/File/PathEndsWith?path=...`: file candidates; matching a suffix is **not** a reliable identity match until the returned accessible `Locations[].AbsolutePath` exactly matches.
- `GET /api/v3/Episode/AniDB/{anidbEpisodeID}/Episode`: exact AniDB episode lookup returning `IDs.ID` (Shoko internal episode ID).
- `GET /api/v3/File/{fileID}/Location?includeAbsolutePaths=true`: file-location verification when needed.

Exact documented link write, **not enabled**:
- `POST /api/v3/File/{fileID}/Link` with JSON object `{"EpisodeIDs":[<Shoko internal episode ID>]}`, not the AniDB episode ID.
- `POST /api/v3/File/{fileID}/LinkFromSeries` requires `SeriesID`, `RangeStart`, and `RangeEnd`; prefer exact ID-based linking to avoid fuzzy episode ranges.

Shoko also exposes destructive endpoints including `DELETE /File/{fileID}`, `DELETE /File/Location`, and `GET /Action/SyncMyList` (documented as irreversible AniDB synchronization). AniKeepOrNot must never invoke these in any automatic workflow.

The feature-branch `anime_shoko_v3.py` client performs only authenticated GETs and prepares a `writes_enabled:false` link plan. API calls and source paths still require VM validation and NFS path mapping. Do not enable automatic Shoko writes until episode IDs and file references have been verified against read-only live responses.


## Post-Keep Shoko reconciliation implementation status (2026-10-19)

The feature branch now starts `anime_shoko_handoff_monitor.py` only when Shoko and Sonarr are enabled. After a series reaches `KEPT`, the monitor reconstructs its AniKeepOrNot provenance, **revalidates the exact Sonarr episode destination and recovery hash**, persists one Shoko handoff record per notification and polls Shoko with GET requests only. These records survive restarts and do not resubmit Sonarr imports.

**Explicit blocking gap:** The existing verified identity cache and `identity.series_map` prove TVDB and **AniDB anime/series** identity, not an exact AniDB episode identifier. Season/episode ordering differs between TVDB and AniDB (including specials). The monitor therefore reports `NEEDS_EPISODE_IDENTITY` rather than inventing a mapping. `record_verified_episode` requires explicit, separately verified provenance-bound AniDB *episode* ID.

**Shoko API contract:** the supplied Shoko 5.3.3.0 OpenAPI specifies `GET /api/v3/ImportFolder/{folderID}/Scan` as a whole-folder scan; despite its HTTP GET method it has side effects, so it is deliberately NOT called by the read-only monitor. It also specifies `POST /api/v3/File/{fileID}/Link` with `{"EpisodeIDs":[<internal Shoko episode ID>]}`. Automatic linking is still disabled. Shoko's observed `SeriesIDs:null` cannot be treated as a proved empty list of existing episode links.

**Not production-ready yet:** Sonarr ManualImport command payload awaits a non-destructive VM simulation; the Shoko scan side-effect policy, exact AniDB-episode mapping, final link writes and readback/VFS verification remain blocked pending trustworthy live contract data. The current Sonarr finalizer marks `KEPT` and may perform source cleanup before Shoko completion; this is not yet the desired final end-to-end state. Do not deploy, release, merge or enable destructive actions.


### Simplified completion policy

Shoko VFS/Jellyfin visibility is **not** an end-to-end prerequisite. Completion instead requires that Shoko read back the exact verified AniDB episode association for each Sonarr-imported file. The Sonarr finalizer now retains trial files and qB source torrents while Shoko is enabled and that readback has not been recorded as `SHOKO_LINK_VERIFIED` or `SHOKO_ALREADY_LINKED_VERIFIED`. No code path currently promotes records to those states: they remain intentionally blocked until precise episode link verification is implemented. This avoids premature cleanup, but means Keep cannot complete automatically with Shoko enabled yet. Disabling Shoko retains legacy Sonarr-only cleanup logic.

Both components consume central configuration from `load_config()`: Sonarr's `settings.sonarr.base_url` and `secrets.sonarr_api_key`, Shoko's `settings.shoko.base_url` and `secrets.shoko_api_key`. Never insert credentials in logs or repo files.


### Installed-system upgrade and settings migration

For an **existing** AniKeepOrNot installation, do not rerun `deploy/install-homelab.py`. Once a tested release is approved and published, run `anime-discovery-admin upgrade`, then `anime-discovery-admin settings migrate` to preview missing keys and `anime-discovery-admin settings migrate --apply` to back up `settings.json` and insert only missing defaults. `secrets.json` is deliberately **not** modified by migration; prior `web_token`, `ntfy_token`, `tvdb_api_key`, `sonarr_api_key` and `shoko_api_key` values remain intact.

New integrations default **disabled**. To supply missing API credentials after migration, use `anime-discovery-admin settings configure-integration sonarr`, `... shoko`, `... tvdb`, or `... ntfy`; entering a blank credential preserves any existing key. No AniDB credential exists in this configuration schema: `identity.series_map[*].anidb_id` is a catalog identifier, not a secret. The installer inherits the same central defaults, so fresh installs include the settings sections without auto-generating credentials.

Do not run these upgrade instructions until the feature branch has completed CI and live validation and a release is explicitly approved; `develop` triggers publication.


### Final lifecycle decision: Shoko is never a Keep or cleanup blocker

This section supersedes earlier notes about gating cleanup on Shoko. **Only successful Sonarr ManualImport, exact independent destination verification, and qB ownership/source checks** determine whether original AniKeepOrNot trial media and torrent data can be cleaned up. When Shoko is enabled, the finalizer attempts to persist post-import provenance, but failures in Shoko status collection do not change the successful Sonarr outcome. It never deletes any permanent Sonarr file.

Shoko reconciliation is a separate best-effort retry workflow after Keep. Issues requiring manual action (`NEEDS_EPISODE_IDENTITY`, `UNRECOGNIZED_NEEDS_REVIEW`, `AMBIGUOUS_SHOKO_FILE`) trigger a deduplicated, best-effort NTFY warning using the existing configured server/topic/token. Delivery failure never blocks Keep. No Shoko VFS/Jellyfin verification is required. An exact verified AniDB *episode* identity remains mandatory before any future link write; identifying the AniDB series alone is not sufficient. Automatic Shoko link execution is not enabled yet.


### Shoko post-Keep integration: implemented API flow (supersedes earlier read-only notes)

The feature branch now implements all four code paths:

1. **Episode identity:** fetches the unique TVDB episode ID through TVDB v4 and correlates it to the unique `IDs.TvDB` cross-reference in the Shoko episode catalog of the independently verified AniDB series. A trusted explicit `identity.episode_map` may be used if cross-references are incomplete. No TVDB season/episode-to-AniDB arithmetic.
2. **Scan:** when a Sonarr-imported permanent file is not in Shoko yet, invokes `GET /api/v3/ImportFolder/{folderID}/Scan` at most once per hour per notification. Before scanning, it confirms the configured folder's Shoko `Path` equals the Sonarr permanent root. It never scans torrent staging intentionally.
3. **Link:** if there is a unique Shoko file at the exact accessible absolute Sonarr destination, no existing episode association, an exact verified AniDB episode mapping, and `shoko.auto_link_verified=true`, it calls `POST /api/v3/File/{fileID}/Link` with the Shoko internal episode ID.
4. **Readback:** `GET /api/v3/File/{fileID}/Episode` must return precisely the expected Shoko + AniDB episode identity. Existing or conflicting links are never silently replaced.

When unavailable or ambiguous, the process records the issue and sends a deduplicated NTFY manual-attention alert. **Shoko never blocks Sonarr Keep or safe cleanup; there is no VFS/Jellyfin check.**

Configure using the existing admin interface after approval and upgrade:

```bash
anime-discovery-admin settings configure-integration shoko
anime-discovery-admin settings set shoko.import_folder_id 1
# Only after reviewing the verified episode mapping and testing with Shoko:
anime-discovery-admin settings set shoko.auto_link_verified true
```

The example folder ID `1` must be independently confirmed as the Shoko destination import folder for `/mnt/media/Anime`, never a torrent folder. The feature defaults `auto_link_verified` to false; the interactive integration wizard also resets it to false as a safe default. Existing installation settings migrate add missing `identity.episode_map` and `shoko.import_folder_id` defaults without touching credentials. A per-episode fallback map entry has the exact form `"TVDB_SERIES_ID:SEASON:EPISODE": {"anidb_episode_id": 12345, "verified": true}`; supply only independently verified values.

**Validation limitation:** CI tests are mocked. A controlled live integration test against the user's actual TVDB, Sonarr and Shoko services is still needed before any release, automatic linking, or production cleanup is enabled. Nothing in the feature branch has been deployed.
