# API v1: management, dashboard data and authenticated intake

The API is a separate ASGI process. Every application endpoint lives beneath
`/api/v1/`; it never runs the jobs worker, provider lookup or acquisition pipeline
inside an HTTP handler. Dashboard and ADM clients use the same management routes
and authorization. No dashboard HTML or supervisor is implemented. Keep requests can be recorded,
but actual media promotion remains blocked until its library is implemented.

## Routes

Application responses are UTF-8 JSON with api_version and a generated request_id.
Transport/proxy rejections before ASGI dispatch may use a plain HTTP error instead.
Success
uses data; errors use the shared reason catalog with code, readable message,
action and translation keys. Responses are no-store and nosniff, with no CORS
allow headers. Stable event/job IDs correlate intake and history independently
of the HTTP request ID. Raw payloads, lease tokens and private credentials are
never returned by management reads.

| Method and route | Authorization | Result |
| --- | --- | --- |
| GET /api/v1/ | Management | Version and available resources |
| GET /api/v1/health | Management | Local readiness/schema check; no provider requests |
| GET /api/v1/services | Management | Last known status of each external service, as saved by the worker (dashboard status bar); no service requests |
| POST /api/v1/webhook | Separate webhook token | Durable normalized event and initial IDENTIFY_ANIME job; 202 only after commit |
| POST /api/v1/auth/session | Management key in JSON plus exact Origin | Browser session cookie and CSRF token |
| GET /api/v1/auth/session | Management Bearer or browser session | Authentication status; cookie sessions may retrieve their CSRF token |
| DELETE /api/v1/auth/session | Management; browser requires Origin and CSRF | Revoke that process-local session and clear its cookie |
| GET /api/v1/settings | Management | Desired/effective settings, restart requirements and field metadata |
| PATCH /api/v1/settings | Management | Validate and save through ConfigurationStore with expected_revision |
| GET /api/v1/credentials | Management | Presence flags and private-file revision only |
| PATCH /api/v1/credentials/tvdb | Management | Replace/clear api_key with expected_revision |
| PATCH /api/v1/credentials/qbittorrent | Management | Replace/clear username/password pair with expected_revision |
| PATCH /api/v1/credentials/access | Management | Replace/clear distinct webhook_token/management_token with expected_revision |
| GET /api/v1/jobs | Management | Bounded job list with readable status/reason and cancellation capabilities |
| GET /api/v1/jobs/{id} | Management | Individual readable status and action availability |
| GET /api/v1/jobs/{id}/history | Management | Bounded readable event/handler history and allowlisted details |
| GET /api/v1/jobs/{id}/cancellation | Management | Cancellation and cleanup state/capabilities |
| POST /api/v1/jobs/{id}/cancel | Management | Cancel idle work immediately, or atomically request cancellation and enqueue cleanup |
| POST /api/v1/jobs/{id}/cleanup | Management | Submit/reuse cleanup control job; explicitly resume that job after review |
| POST /api/v1/jobs/{id}/keep | Management | Record one Keep control job for a staged operation. Refused with the reason (503 `SERVICE_UNAVAILABLE`, or 409 `KEEP_CONFIGURATION_INVALID`) while Sonarr's saved status says it cannot work; see services/README.md |

Job/history reads accept after_id (default 0) and limit (1–100, default 50), and
return next_after_id. The list is ordered by increasing durable IDs. A callback
may return changed capabilities between reads; cancellation rechecks current
state atomically, so a stale UI cannot cancel STAGED work. No lease/payload editing
or arbitrary handler enqueue endpoint exists.

Settings PATCH body: `{"expected_revision":1,"settings":{"logging":{"level":"DEBUG"}}}`.
Use the revision returned by the latest GET. Credential PATCH bodies contain
expected_revision and exactly the named credential fields; empty strings clear
credentials where the shared library allows it. Public responses contain presence,
saved and durability_confirmed, never the supplied values. A saved-but-unconfirmed
publication is reported accurately, rather than as a misleading unsaved error.
Clearing the management key closes management access until local bootstrap.

Keep, cancel and cleanup accept only `{}`. No caller-supplied job kind, module,
path or arbitrary payload is accepted. Bearer authorization and browser
Origin/CSRF checks apply to all three. See
[MANAGEMENT_ACTIONS.md](../MANAGEMENT_ACTIONS.md) for the durable action contract,
HTTP 202 receipt, polling, duplicate-click handling and explicit Keep limitation.
HTTP handlers never call torrent/file cleanup. GET never starts work.

A local cancellation with no running handlers and no recorded resources returns
200 with completed=true and a readable notification. All queued action receipts
return 202 only after commit, with job_id, status_url, history_url, current
state/status, translation-ready notification and disable_action=true. Location
contains status_url. The response acknowledges submission, not completion.
A repeat returns the existing job, even if it has finished or needs attention;
POST cleanup after review explicitly resumes that same cleanup job. Journal
uncertainty never rearms a destructive command. See
[CANCELLATION.md](../CANCELLATION.md) for file/drain protections.

Configuration/credential revision conflicts return 409. Authentication failure
returns 401; browser Origin/CSRF failures return 403; malformed/oversized input
returns 400/413; unsupported formats return 415. Temporary maintenance/storage
failures return 503. Auth/session limits return 429. The authenticated local health
check does not imply provider credentials, metadata retrieval, media verification
or staging are operational.

## Webhook authentication: preserve the legacy transport contract

The legacy anime-discovery-web.py specifically accepted Autobrr's token in its
JSON object, as well as Bearer and query-token alternatives. NextGen preserves all
three at the new route. They grant intake permission only:

```json
{
  "token": "YOUR_SEPARATE_WEBHOOK_KEY",
  "release_name": "Example Anime - 10",
  "torrent_infohash": "0123456789abcdef0123456789abcdef01234567"
}
```

Alternatively omit body token and use `Authorization: Bearer YOUR_WEBHOOK_KEY`,
or `/api/v1/webhook?token=YOUR_WEBHOOK_KEY`. Prefer body/header credentials.
Duplicate query tokens and conflicting credential locations are rejected, even
if one value is correct. Duplicate JSON keys and duplicate HTTP headers are
rejected. Invalid/non-string/empty credentials do not fall back to a weaker path.
A browser session or management key does not authenticate webhook intake, and the
webhook key never authenticates settings, jobs, credential changes or cleanup.

The current endpoint accepts release_name/torrentName/name and
torrent_infohash/info_hash/torrentHash/hash aliases, rejecting contradictory
values. Only a nonblank bounded release name and a supported 40-hex v1 hash are
retained. Strip credentials and discard unknown fields before persistence; do not
store raw webhook JSON, request URLs, headers, arbitrary source labels, magnet or
tracker URLs. Known access keys embedded in the retained declaration are rejected.
External anime/TVDB IDs and any verified/approved flag are not trusted evidence.
The actual authenticated adapter labels the source autobrr and atomically records
ACCEPTED/CREATED history as component api. Each accepted delivery remains a distinct
incoming event; this is not legacy GUID-based deduplication or automatic approval.
Later exact episode/resource gates arbitrate duplicate work.

Missing/unreadable/unsafe secrets fail closed. Read current credentials on every
request; do not cache an old token after rotation or fall back to legacy files,
environment credentials, a query credential on a management route, or a default
password. No legacy /webhook or /api route alias is silently installed.

## Management and browser security

Central secrets v3 adds separate URL-safe management_token and webhook_token
(32–256 characters; use high-entropy password-manager-generated values). They must
be different. V1/v2 remain readable without implicit writes. Explicit access-key
updates upgrade to v3 while preserving TVDB/qBittorrent credentials, revision
checking, owner/mode/symlink protections and atomic publication. Later provider
updates preserve v3 and the access keys. Public credential status is presence only.
No password, OIDC, user enrollment, multi-user roles or legacy auth-file migration
is claimed by this first API foundation.

ADM uses `Authorization: Bearer MANAGEMENT_KEY` without a cookie. A future dashboard
can exchange that management key through POST auth/session, then use the HttpOnly,
SameSite=Strict cookie and X-CSRF-Token for mutations. Login requires an exact
Origin; browser mutations require it plus the session-bound CSRF token. Origin,
Host and Sec-Fetch-Site checks do not trust Forwarded/X-Forwarded-* headers. Other
origins receive no CORS grant. HTTPS public origins produce Secure cookies; local
loopback HTTP is supported for development only.

Sessions are random opaque IDs, hashed in a bounded process-local store; CSRF and
management-key fingerprints are kept separately. They expire after 12 hours,
logout removes the current session, and management-key rotation invalidates old
sessions on their next request. API restart logs all browser sessions out.
Already authorized in-flight operations are not rolled back by rotation. This is
a single API process contract; multiple ASGI workers require a shared durable
session design first. Login limits use the direct transport peer, deliberately
ignoring spoofable forwarding headers; a proxy may therefore share that bucket.

## Process, transport and permission boundary

The optional transport dependencies are in requirements-nextgen-api.txt. The
launcher uses Uvicorn/h11, not the legacy stdlib HTTP server. It binds only to
loopback, uses one process with bounded concurrent connections, disables WebSockets,
proxy-header trust, automatic request/access logging and transport TRACE, and
bounds incomplete protocol data. Application headers/query/body/pagination and
session stores are bounded; JSON body collection has a 15-second deadline and a
64 KiB maximum. Framing belongs to the maintained ASGI transport, with additional
application checks for duplicates, ambiguous lengths and encoded route aliases.
No package has been installed or server started for this PR.

The API dispatches blocking configuration, SQLite and cleanup work through a
bounded executor using shared cancellation-drained I/O. Dropping/canceling a
request does not release a worker thread's maintenance/ownership gates early.
The Worker re-exports that shared helper for existing library callers. Settings
use one fresh operation snapshot per management request; host/port/database path
remain pinned to startup until explicit restart. Most other settings apply to
new operations. CancellationService owns its admission, so the API must not nest
another maintenance gate around it. Reads/writes honor the existing maintenance
protocol; no implicit DB initialization, SQL migration, service restart or
permission repair occurs.

The service account owns configuration/secrets/database access. Dashboard/ADM
clients call this API instead of writing those files themselves. The API calls
the validated shared libraries; it does not accept arbitrary filesystem targets
or run the worker inside the API process. Cancellation already authorizes only
the ownership-proven resources recorded by that operation.

Explicit local bootstrap (commands shown only; not executed):

```sh
python -m src.nextgen.api init-access --config-dir /var/lib/anime-discovery/nextgen
python -m src.nextgen.api serve --config-dir /var/lib/anime-discovery/nextgen --public-origin https://anime.example
```

The first command prompts without echo for two distinct generated keys and saves
them in the central private file, preserving existing provider credentials. The
directory and validated settings/current NextGen database must already be prepared
through their explicit libraries/install workflow. Running the server does not
bootstrap access or migrate anything. Public origin is an explicit startup option;
it must match the reverse-proxy dashboard origin and requires restart to change.
Publish the loopback service only through a correctly configured TLS reverse proxy.

**Query-token compatibility needs path-only/redacted proxy access logs.** Application
and launcher logs never record the request URL/query, headers, cookies, body or raw
exception text; third-party transport notices are reduced to a static catalog
message. A reverse proxy sits outside those controls. Preserve authentication
headers/body while redacting query strings at every proxy/logging layer. Do not
put secrets in metrics labels or report a captured request as an error payload.

## Validation and remaining work

Readable event/reason codes use the shared catalog and translation keys. Offline
cases cover the three legacy token locations, role isolation, conflicting/duplicate
credentials, missing/unsafe storage, rotation/session revocation, cookie Origin and
CSRF, bounded inputs, settings conflicts/restart behavior, presence-only credential
responses, sanitized durable intake, jobs/history and authenticated cancellation.
They intentionally remain **unexecuted**, along with all CI. No application import,
listener, SQL execution, provider/client call, live cleanup or deployment was run.

Dashboard HTML, password/OIDC sign-in, full management migration/maintenance routes,
ADM transport, supervisor process control, metadata retrieval and later media/Keep
routes remain separate work. Do not present them as implemented by this API.

References checked during design:
- [Legacy webhook handler](https://github.com/bugz8unny69/anime-discovery/blob/3dd097a9ae6d5914d0dccbad41750c02afdf4bea/src/legacy/anime-discovery-web.py)
- [ASGI HTTP specification](https://asgi.readthedocs.io/en/latest/specs/www.html)
- [Uvicorn configuration](https://www.uvicorn.org/)
- [Python constant-time comparison](https://docs.python.org/3/library/hmac.html)

## Reused dashboard

The same ASGI process now serves `/`, `/jobs`, `/settings` and allowlisted local
dashboard assets. See [the dashboard contract](../dashboard/README.md). Sign-in
uses the existing management browser session, Origin and CSRF rules. Webhook
authentication is unchanged. Serve dashboard and API paths on the same configured
public origin; no private data is embedded in the unauthenticated shell.

GET `/api/v1/intake` is a bounded newest-first acquisition view, one latest member
per explicit operation, excluding control operations. It accepts `before_id`
(operation cursor) and `limit` (1–100). GET `/api/v1/jobs` additionally accepts
`before_id` for descending job pagination; existing `after_id` behavior remains
ascending. Cursors cannot be mixed. History retains ascending `after_id` only.
Responses supply `next_before_id` or `next_after_id` for the chosen direction.
Status filters in this first dashboard apply only to the current page.

Job views now include bounded release/title/timestamp context, a trusted readable
handler label, catalog-keyed acquisition status and `is_management_action`.
They never return raw bodies, source URLs, arbitrary payloads, credentials or
leases. A job marked DONE is still distinct from successfully committed staging.

## Reject

Reject applies to a whole series only: POST `/api/v1/intake/series/<anidb_id>/reject`
from the Intake dashboard (see docs/INTAKE_SERIES_BEHAVIOR.md). The former
per-job `/jobs/<id>/reject` action, REJECT_MEDIA and REJECT_BATCH were removed.

## Keep execution and Sonarr credentials

KEEP_MEDIA now runs the Sonarr import service; execution_available is true.
The existing authenticated POST jobs/{id}/keep returns a durable job receipt,
resumes the same stopped job on explicit retry and reuses successful results.
Keep receipts expose completion with a catalog-keyed Kept message only after the
saved import proof is complete. PATCH credentials/sonarr accepts api_key and
expected_revision; credential reads add sonarr_configured, never the saved key.
See [Keep/Sonarr](../KEEP_SONARR.md). Webhook and browser authorization are unchanged.

Jellyfin uses PATCH /api/v1/credentials/jellyfin with api_key and
expected_revision, with the same session/CSRF/revision contract as Sonarr.
GET credentials adds jellyfin_configured, never the key. Settings expose
jellyfin.base_url and jellyfin.timeout_seconds. See ../JELLYFIN_REFRESH.md.

## Decision download cleanup follow-up

New completed Reject decisions and post-Keep staging cleanups enqueue a separate
owned-download cleanup job. Receipts expose `download_cleanup`; a stopped job
can resume through Keep/Reject with the dashboard label Retry download cleanup.
Decision/import completion and episode protection remain intact. Current contract:
[Download cleanup](../DECISION_DOWNLOAD_CLEANUP.md). This supersedes earlier remaining-download-cleanup notes above. Tests remain unrun.

## Optional Shoko and service startup

The dashboard now exposes the optional Shoko address/timeout and private key
replacement through PATCH `/api/v1/credentials/shoko`. Keep receipts include
`optional_shoko` metadata/status, with explicit retry without another import.
See [Shoko](../SHOKO_METADATA.md) and [Service setup](../SERVICE_SETUP.md).
The API command checks local readiness before serving; no implicit initialization,
upgrade or worker startup occurs. Tests/CI and service startup remain unrun.
