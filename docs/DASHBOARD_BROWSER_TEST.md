# Dashboard browser test (step 3, for later)

Status: not started. Written 2026-11-27, after the dashboard cleanup.

## Why

After the series job replaced the per-step jobs, `dashboard.js` (about 2,100 lines,
written for the old pipeline) still had code wired to the removed Jobs page, its
elements and its API routes. In production every error stopped the whole script, and
each fix only showed the next one.

Steps 1 and 2 are done:

1. **The dead code is gone.** The Jobs list, bulk actions, job history, job matching
   and coverage, the transcode batch panel and the old series view are removed. The
   script went from 2,122 to about 1,350 lines.
2. **Static checks run in the normal test suite** (`src/anime/tests/test_dashboard_static.py`):
   - every element ID the script looks up is in `index.html`;
   - every API path it requests is a route `app.py` serves.

   Run against the old script, they report all 15 missing elements and 10 dead API
   paths at once. `dashboard_load.js` also runs the script at load for each of the
   four pages against the real page's IDs, in Node.

What those don't cover: **what the API returns, and how the pages draw it.** A field
that is renamed or no longer sent, a rendering bug, a button handler that fails when
clicked. Do step 3 when one of those shows up.

## Step 3: every page in a real browser against the real API

- Start the real `Application` (as `test_dashboard_series_api.py` does) on a local
  port, with a test database seeded with series in each state: working, asleep
  (waiting with `wake_at_ms`), stopped by a program error (`work.stopped`), a torrent
  stopped (`why`), staging full (`STAGING_FULL`), staged, Keep under way and stopped,
  kept, rejected. Seed a few notifications of each type with actions.
- Open each page (`/`, `/running`, `/recognized`, `/settings`) in a headless browser:
  Firefox is installed on the development machine (Marionette or geckodriver), or
  Playwright with Chromium in CI.
- Sign in, then on each page: open a series, open the bell, run each bell action, open
  Settings sections. Fail on any uncaught error in the console, any request that
  answers 4xx/5xx (other than expected refusals), and any element that should be
  visible and is not.
- Keep it in the suite, skipped when no browser is installed (like `dashboard_load.js`
  without Node).
