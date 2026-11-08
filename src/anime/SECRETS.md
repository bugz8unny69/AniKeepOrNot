# Required provider settings and private credentials

AniDB and TVDB are required by the identity worker. There are no provider enable
flags. TVDB authentication uses only an API key; the previous optional PIN model
has been removed from Credentials, configuration and login request construction.
A provider may still reject a key at runtime; that produces SERVICE_SETUP_INVALID.

## Public configuration

Configuration v4 adds these next-operation fields:

| Field | Default | Readiness requirement |
| --- | --- | --- |
| anidb.client_name | Empty | Registered name, 3–32 allowed characters |
| anidb.client_version | 1 | Integer 1–999999 |
| providers.timeout_seconds | 30 | Integer 1–60 |

Blank client name supports initial setup but cannot pass identity-worker
readiness. Explicit v3→v4 conversion adds defaults and increments revision;
historical v0→v1→v2→v3 outputs retain their original key/default shapes. No
SQLite change is needed (database is now v14; AnimeSeries remains v2).

## Private credential repository

SecretStore(config_directory) manages a separate secrets.json envelope with
schema_version=2, a positive independent revision and a credentials object
containing tvdb_api_key plus qbittorrent_username and qbittorrent_password.
Existing v1 TVDB-only files remain readable; see the compatibility section below. An empty key means unconfigured/explicitly cleared.
This is a new NextGen format, not an automatic import of a legacy secret file.
The TVDB key is never accepted in settings.json, jobs, diagnostic history or SQL.

- initialize(tvdb_api_key="") explicitly creates a new file and refuses replacing
  an existing file. Reads never create files. ConfigurationStore.initialize does
  not implicitly create secrets or perform network requests.
- update_tvdb_api_key(value, expected_revision=...) atomically replaces or clears
  the key. Revision conflicts prevent overwrites; unchanged values do not rewrite.
- status() returns only revision, tvdb_api_key_configured and qbittorrent_configured. Mutation results
  expose this status plus saved/durability fields; they contain no secret values.
- tvdb_credentials() is a trusted internal provider method returning a Credentials
  object with its API key hidden from repr. Never serialize this object into an
  API response. Public management handlers should use status(), not this method.

The file must be service-owned, regular, single-link and inaccessible to group
or other users. Reads reject symlinks/FIFOs/nonregular files and unsafe modes.
New temporary/final secret files use mode 0600. The parent directory must be
service-owned and not writable by group/others.

Read-only access does not require the reader to own the file or the directory.
Root can read secrets this way, for example through the internal admin API.
The private mode, single-link and directory-writability checks still apply,
and the kernel's own permission check still blocks other accounts. Writes
(`initialize` and updates) still require the caller to own both, so a publish
never replaces the service-owned file with a file owned by root or anyone else.
Installation must establish
these permissions; the library never repairs ownership or changes installed
file permissions to bypass errors.

Publication uses the existing stable settings lock, same-directory temporary
files, file fsync, atomic replace and directory fsync. Failure before replace
preserves the old file. Failure confirming directory durability after replace
reports saved=True/durability_confirmed=False with the existing readable reason;
callers must inspect current status before retrying. No plaintext backup files
are created. Error messages contain no values, paths or raw filesystem errors.
Settings and secret revisions are independent; there is no claim of a two-file
atomic transaction. Management can stage their updates while work is paused.

## Worker composition

await build_identity_worker(config_directory, ...) validates local startup
settings and the TVDB key before constructing the worker. The returned worker is
unstarted and must be served or closed by its owner. run_identity_worker supplies
the worker's existing SIGTERM/SIGINT handling, bounded dispatch and draining.
These are library entry points, not installed systemd services or subprocesses.

The composed worker checks fresh settings/credential availability under
maintenance admission before every claim. Missing/unsafe credentials or an
unconfigured AniDB client raise a static reason-coded configuration error,
leaving queued jobs untouched. The polling loop stops/drains for correction.
Startup/readiness validation does not log in, validate the remote API key or
send any provider request. The identity job later performs both provider checks.

The resolver captures Options and Credentials once per attempt before contacting
providers. Running work keeps those values; later attempts see changes. Up to
four option-specific clients remain cached without mutating in-flight clients;
provider-wide SQLite pacing and public caches remain shared. TVDB memory cache
keys include the API-key fingerprint so rotated credentials do not reuse old
credentials' token/response entries. Old entries expire/evict normally; secret
values and bearer tokens are never persisted or logged.

There is no legacy environment fallback, TVDB PIN, provider disable switch,
authenticated management endpoint, ADM command or supervisor implementation in
this change. Future management routes call these service-side libraries under
the service account with their own authentication/authorization.

Offline cases are authored but unexecuted under project policy. No application
imports/execution, SQL, CI/tests, live provider calls or deployment occurred.


The composed identity worker now also installs VERIFY_RELEASE and shares a
ClientPool across both handlers. Its successful declared-episode verification
stops before trial policy/resource claims or acquisition. Additional trusted
registrations remain explicit; no directory scan or payload routing is added.

## Download credential extension

The central store now writes secrets v2 with a qbittorrent username/password pair
and tvdb_api_key. Public status includes qbittorrent_configured only. v1 remains
readable and is unchanged by reads/TVDB-only updates; explicit pair changes
upgrade it atomically. Existing private-file checks and no-backup policy remain.
See DOWNLOAD_CONFIGURATION.md for exact compatibility and point-of-use capture.

## API access keys: secrets v3

Explicit initialize/update_access_tokens adds distinct webhook_token and
management_token fields (32–256 URL-safe characters, or blank to disable).
access_credentials() is trusted private access with hidden repr; status() exposes
only presence flags/revision. Values live in the same private secrets.json, never
settings, environment fallback or public GET responses. Existing v1/v2 reads do
not write; adding access keys explicitly upgrades to v3 and later provider updates
preserve it. Empty access keys retain old document shapes when no upgrade occurs.

The webhook key grants intake only. Management Bearer access or an authenticated
browser session grants management; the API never promotes a webhook credential
to an administrator password. Browser mutation requires Origin and session-bound
CSRF. Rotation is read on every request and revokes old-key browser sessions on
next use. Bootstrap is local and explicit, without echoing keys. See
[API v1](api/README.md) for the supported token locations and proxy log rules.

## Sonarr credentials, secrets v4

Sonarr's API key is private sonarr_api_key. SecretStore.sonarr_api_key obtains it
for trusted import code; update_sonarr_api_key revision-checks replacement/clear.
PATCH /api/v1/credentials/sonarr accepts expected_revision and api_key with the
existing management/CSRF contract. Public status adds sonarr_configured only.
v1–v3 stay readable; Sonarr writes upgrade to v4 and preserve other credentials.

## Current Shoko and service setup contract

The latest draft adds optional Shoko AniDB metadata after Keep (configuration v10,
backward-readable secrets v6; SQLite stays v23). It also provides explicit local
setup/maintenance commands and composed worker/systemd examples. See
[Shoko metadata](SHOKO_METADATA.md) and [Service setup](SERVICE_SETUP.md).
These supersede earlier remaining-Shoko/supervision/version notes above.
Tests, migrations and services have not been executed.
