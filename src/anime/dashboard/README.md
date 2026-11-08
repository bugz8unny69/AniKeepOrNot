Current Keep behavior: eligible staged work can now run Keep through Sonarr.
The dashboard shows the API's durable Kept result and permits explicit resume
of stopped Keep jobs. See [Keep/Sonarr](../KEEP_SONARR.md). This supersedes the
older unavailable-execution boundary described below.

# Dashboard using API v1

The API process serves the browser dashboard at `/`, `/jobs` and `/settings`,
plus an exact allowlist of local CSS/JavaScript assets. Use the existing API
launcher and configured public origin. Publish the root paths and `/api/v1/`
through the same HTTPS origin; proxying only `/api/v1/` is insufficient.
No extra dashboard process, dependencies, build step or database is required.
The current `serve` entry point now serves both the dashboard and API.

The dark shell, navigation, responsive panels, jobs table, status badges and
DOM-based rendering are adapted from legacy `anime_dashboard_pages.py`
(source blob `651e163a87ff96d0cbc66b0cb5d33b65135091d4`). The settings forms retain
the schema-driven pattern from `anime_dashboard_settings_page.py`
(blob `5861a87e8af7ace2b950e5b4a96dd5b5c0c4c359`). These are reused presentation
patterns and styles, without importing legacy runtime code, reading legacy
SQLite/JSON, invoking legacy endpoints or shelling out to media scripts.

## Browser behavior

- Sign in with the management access key using the existing session endpoint.
  The browser uses its HttpOnly cookie; the CSRF token lives in page memory.
  Mutations send `X-CSRF-Token`. No access key or session token is written to
  browser storage, URLs or generated HTML. Reload restores the existing session.
  Credential rotation/expiry prompts sign-in and clears displayed private data.
- Intake shows the newest job in each explicit acquisition scope. It preserves
  separate deliveries and excludes management-control scopes; no job provenance
  or shared anime identity is treated as a workflow grouping rule.
- Jobs shows individual jobs, including control jobs, newest first. Both views
  use bounded cursor pagination. Status filtering applies to the current page;
  polling refreshes that page every five seconds while the document is visible.
  The Live indicator describes dashboard polling, not proof of worker readiness.
- Job/history text and action explanations come from the shared API catalogs.
  Release names and other values are inserted as text, never HTML. History is
  loaded on demand with bounded continuation pages; action-job history is separate.
- Keep/Cancel/Cleanup obey API availability. Disabled buttons have a focusable
  wrapper with a tooltip and accessible explanation. Queued/running action jobs
  disable competing buttons with their actual explanation. Submission immediately
  disables buttons locally and displays the API's notification, such as
  `Action submitted.` A response is never interpreted as completed background work.
  Uncertain requests cause status reads, never automatic mutation retries.
- Cancel is offered before successful staging; the STAGED milestone closes it.
  A completed queue job is not described as staged. Keep remains disabled because
  `execution_available=false`; this UI cannot make media promotion operational.
- Settings render from API field metadata and use expected revisions on PATCH.
  Dirty input is preserved during polling and prompts before navigation/refresh.
  Inputs are disabled during saves. Revision conflicts require an explicit refresh;
  there is no silent merge or overwrite. Infrastructure restart requirements and
  unconfirmed storage durability are reported separately.
- TVDB/qBittorrent credentials can be explicitly replaced using revision checks.
  Reads show presence flags only; saved values are never revealed. Access-key
  rotation remains in the existing management API/local bootstrap contract.

The public shell contains no private data; all data requests require management
authorization. Assets use no-store, nosniff, no-referrer and a same-origin CSP
without inline/eval scripts or external assets. Exact host/raw-path/request bounds
still apply. Webhook body/Bearer/query compatibility and separate credentials
are unchanged. Proxy URL logs must still omit webhook query strings.

## Scope and validation

Reject/reconsideration, manual discovery, identity correction, connection tests,
Sonarr cleanup review, recognized-name editing, worker supervision and the actual
staging/Keep pipeline need their own supported NextGen contracts. Legacy controls
for those actions are not attached to old endpoints or made to appear functional.
No runtime schema or configuration migration is introduced here.

Authored offline cases cover shell/asset policy, private-view authentication and
CSRF, operation grouping, retained duplicate deliveries, newest/legacy pagination,
sanitized display data, disabled-button explanations, duplicate clicks, uncertain
mutations, XSS-safe text and session expiry. Python cases are in
`tests/test_dashboard_api.py`; `tests/dashboard_browser.test.cjs` uses Node's
standard test/VM libraries and a minimal DOM/Fetch double. It is not a real browser.
Both suites and CI remain unexecuted under current project policy. Source/syntax
checks do not prove browser layout, API integration or runtime behavior.

Later authorized UI checks: real-browser sign-in/expiry, mobile tables and dialogs,
keyboard tooltips, settings conflicts, request-loss recovery and polling alongside
active jobs. No live listener, provider/client request, cleanup or deployment has
been performed for this change.

## Reject follow-up

Reject lives only on the Intake series dialog and rejects the whole series
(episodes, seasons, specials and OVAs together). The per-job Reject button was
removed.

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
