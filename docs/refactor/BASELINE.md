# Refactor baseline (RF-001)

This records the starting point for the NextGen refactor (work items RF-001 through RF-009). Later results are measured against it.

## Base revision

| Item | Value |
|---|---|
| Upstream branch | `NextGen` (`origin/NextGen`) |
| Base commit | `99dd30ef48b3fc4a67f3931ec9c454db726cbfc9` |
| Base commit subject | Merge pull request #255 from bugz8unny69/feat/historical-job-recovery-operations |
| Refactor branch | `refactor`, created from the base commit with no other changes |
| Worktree | Separate worktree `../anime-discovery-refactor`. The primary checkout stays on `NextGen`. |
| Recorded | 2026-11-04 |

No functional code was changed when the branch or this baseline was created. The only other change in this work item is a documentation update to the test policy (see below).

## Execution environment

| Component | Version |
|---|---|
| OS | Arch Linux, kernel 7.2.9-arch1-1 |
| Python | 3.14.8 |
| SQLite (Python `sqlite3`) | 3.54.0 |
| Node.js | v26.11.1 |

The supported Python range in `nextgen-ci.yml` is 3.11 to 3.13. This baseline ran on 3.14.8, which is newer than that range. All tests passed on it.

## Baseline results

All commands ran from the repository root, offline, with no live services.

| Check | Command | Result |
|---|---|---|
| Complete Python suite | `python -m src.nextgen.tests.run_batches` | **30/30 batches passed. 1,483 tests, 0 failures, 0 errors, 0 skipped.** Wall time 2m41s |
| Test inventory | `python -m src.nextgen.tests.run_batches --list` | 146 test modules |
| Python syntax | `python -m compileall -q src/nextgen` | Passed (exit 0) |
| Dashboard JS syntax | `node --check src/nextgen/dashboard/static/dashboard.js` | Passed (exit 0) |
| Dashboard offline tests | `node --test src/nextgen/tests/dashboard_browser.test.cjs` | **25/25 passed**, 0 failed |

The baseline has no known failures. A failure seen later in the refactor is a regression, unless it can be reproduced on the base commit.

## Source size at baseline

`src/nextgen`, excluding tests: 167 Python files, 27,589 lines. RF-003 and RF-008 report reductions against these figures.

## Test policy

Tests are no longer deferred. Every work item runs the complete local suite and fixes failures before it is committed, pushed and opened as a PR. GitHub Actions is not used for routine verification. `development.md` and `src/nextgen/tests/README.md` were updated to say this. The CI workflow file itself was left unchanged on purpose.

## Recurring upstream synchronization (RF-009)

Before each work item, and after significant changes land on `NextGen`:

1. Run `git fetch origin` and review `git log refactor..origin/NextGen`, noting which subsystems are affected.
2. Merge `origin/NextGen` into `refactor`. Do not rebase, so that each incorporated upstream SHA stays visible in the history.
3. Resolve conflicts with `NextGen` as the authority for behavior. Keep refactor's structural changes only where they don't alter behavior. Never pick a side just because it is the refactor side. Document any conflict that can't be resolved with confidence in the merge commit and the PR, rather than guessing.
4. Run the affected tests, then the complete suite.
5. Record the incorporated upstream SHA in the table below.
6. Update `DEAD_CODE_AUDIT.md` and `NAMING_MAP.md` if the new upstream code adds references they cover.

### Incorporated upstream revisions

| Date | `origin/NextGen` SHA | Notes |
|---|---|---|
| 2026-11-04 | `99dd30ef` | Base. No upstream changes beyond the base. |

## Work-item PR flow

`refactor` is the integration branch for the whole refactor and is published to `origin`. Each work item, or batch of items, is developed on its own branch and merged into `refactor` through a PR. `NextGen` is not changed until the final refactor is approved (RF-009). The final `refactor` → `NextGen` PR needs explicit permission.
