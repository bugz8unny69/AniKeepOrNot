# NextGen dashboard and management authentication audit

Status: incremental implementation and source inspection, not deployment verification (2026-10-24).

## Existing NextGen dashboard
- `src/anime/dashboard/assets.py` allowlists `/`, `/jobs`, `/settings`, and static CSS/JS assets.
- `src/anime/dashboard/static/index.html` contains Intake, Jobs, Settings, login, and job-history panels.
- `src/anime/dashboard/static/dashboard.js` implements job polling/actions, history, settings and private credential updates using `/api/v1/`.
- `src/anime/api/app.py` serves these assets through the ASGI API, and implements management routes.

## Gaps before declaring dashboard feature-complete
1. Replace management access-key login with password login, retaining cookie-session and CSRF boundaries.
2. Add Settings > Security: change password, verify current password, clear old sessions.
3. Add local `adm set-password` recovery using a permission-restricted Unix socket served by the existing API process (no extra systemd service). Verify Unix peer credentials and fail closed.
4. Use fresh NextGen configuration and existing configuration library; no legacy secrets, settings, or management-token migration. Provision dashboard password through local `adm set-password` and use the management API for subsequent configuration.
5. Remove or restrict legacy HTTP management-token authentication and the access-token mutation endpoint once password flow is operational.
6. Test unauthorized local/remote HTTP, CSRF, credential rotation, concurrent password changes, and socket permissions.
7. Compare NextGen dashboard workflows against the legacy application's complete feature inventory; presence of three tabs does not prove parity.
8. Verify real browser navigation, TLS proxy behavior, and API/worker outage presentation in an integration environment.

## Security constraint
Do not deploy the scrypt foundation as a completed authentication migration. It is currently only a helper and tests; password-backed API authentication is implemented; integration and host validation remain outstanding.

## Delivery tracker (CI intentionally deferred until requested)

- [x] Worker and API systemd readiness/watchdog implementation (previous CI #18 passed).
- [x] Source-level dashboard route and feature audit.
- [x] Versioned scrypt hash helper and initial tests (not integrated).
- [x] Add separate password-hash persistence with atomic updates and concurrent revision handling (fresh NextGen setup; no migration).
- [x] Implement password-based login and password change; invalidate all sessions on rotation (integration tests pending).
- [x] Remove management-token browser login and bearer-token bypass from administrative HTTP routes (legacy token storage cleanup pending).
- [x] Implement same-process Unix socket (0600, root/service UID), peer identity checks, and `adm set-password` (host validation pending).
- [x] Wire dashboard login and Settings > Security forms to the new API (browser testing pending).
- [x] Remove management-token bootstrap dependency; initialize fresh NextGen secrets v7, provision password via `adm set-password`, and retain independent Autobrr webhook credentials.
- [ ] Implement native NextGen Series, recognized aliases, manual trial requests, cleanup review, identity diagnostics and connection checks; verify worker-unavailable UX. Source comparison documented below.
- [ ] Complete authorization, CSRF, socket, rotation, lifecycle, and browser/API integration tests (new fixtures and regression cases committed; full suite unexecuted).
- [ ] Review complete diff, then ask for approval to run CI; fix failures after approval.

**Gate:** No CI trigger, merge, or deployment until the user approves CI after checklist completion.

## Confirmed scope decision

No migration from the older application is requested or authorized. Treat the legacy source solely as a reference for feature parity. A new NextGen installation starts with fresh configuration, using the existing configuration library and management API for configuration. `adm set-password` bootstraps the administrator password; it does not import legacy secrets. Do not implement migration scripts or automatic legacy-data conversion.

## Feature parity review against the retired dashboard (reference only)

The older source is **not imported, executed, or migrated**. Its browser routes and page functions are used only to identify product capabilities still needed:

| Retired dashboard capability | NextGen status at source review |
| --- | --- |
| Jobs list, status, history, keep/reject/cancel/cleanup | Present under Intake/Jobs, needs browser/API test |
| Configuration forms and provider credentials | Present under Settings, needs full schema validation |
| Password sign-in and security page | Implemented on new API, needs proxy/browser validation |
| Series overview and per-series detail | Not yet implemented in NextGen dashboard |
| Recognized names and alias maintenance | Not yet implemented in NextGen dashboard |
| Manual one-time anime trial request | Not yet implemented in NextGen dashboard |
| Sonarr cleanup review/preview | Not yet implemented in NextGen dashboard |
| Identity trace and diagnostic views | Not yet implemented in NextGen dashboard |
| Provider connection checks in settings | Not yet implemented in NextGen dashboard |

The last six capabilities require native NextGen application/service operations and management endpoints. Do not point the NextGen UI at the retired Python HTTP handlers. Parity is **not complete**, and source review is not a browser validation.

### Fresh-install invariant

There is no conversion or migration step. Use the NextGen configuration library and management API. A fresh API can start before the administrator password or webhook key exists, `adm set-password` bootstraps authentication via the same-process local socket, and the dashboard configures the separate webhook key.

## Automatically recognized series and seasons (implementation started)

On verification of a declared episode, NextGen now remembers a `(TVDB series, TVDB season) -> AniDB anime` record, the provider-verified title, safe normalized release aliases, and the original release verification ID in schema v24. The Recognized Names page lists these records with the count of distinct verified episodes. The identity resolver checks an **exact season-qualified** alias first and, on a unique proven hit, reuses the series relationship without requerying AniDB titles/anime metadata/Anime-Lists or TVDB series metadata. The downstream VERIFY_RELEASE job **still independently validates the episode**, and other acquisitions retain their existing verification and gating.

Unresolved cases fail closed: unknown titles, colliding aliases, contradictory season mappings, mixed season labels, and season 2+ without a previously verified season binding. Releases without an explicit season label continue through the existing provider path rather than presuming an arbitrary season. No manual "Remember" step, legacy settings import, or per-episode identity key is used.

Pending before declaring this feature complete: proper TVDB/AniDB mapping for multipart seasons, specials, sequel anime, alternate numbering orders, refresh/revocation of stale provider proofs, conflict review workflows, richer recognized-alias management, bounded API pagination, and actual integration/browser verification. New tests are authored but **not executed** per CI gate.

## Exact episode placement reuse (v25 implementation)

A second, independent table now stores verified episode placements underneath a recognized series/season: AniDB anime ID and AniDB episode number/ID, TVDB series ID, TVDB season/episode/episode ID, immutable source verification reference, and timestamp. It is populated in the same worker transaction as the release proof and season recognition; conflicting numbering or reused IDs raise a review conflict and roll back.

On later releases, the release verifier first parses a **single, unambiguous** episode declaration and searches the local placement catalog. A matching `SxxExx` looks up exact TVDB season/episode; an explicitly parsed AniDB number looks up that AniDB episode number for the already identified AniDB work. A hit returns the previously verified placement without network calls; a miss uses the full existing AniDB/Anime-Lists/TVDB verification path. No guessed offsets, batch coverage, manual alias authority, download permission, or inferred episode ranges. User cancellation still preempts cache reuse.

The authenticated `/api/v1/recognized/<TVDB series>/<season>/episodes` endpoint and dashboard expansion render the nested verified episodes. Limits currently reject more than 500 rows rather than silently truncating; pagination is pending.

**Still open:** explicitly managed proof invalidation/revalidation and correcting remaps, multipart season support, provider-side changes and freshness policy, recognizing unlabeled sequels, browser testing, end-to-end replay and CI. This is not yet validated and no CI was triggered.
