# Keeping removals removed across NextGen merges

The refactor deletes unused code and renames things, while `NextGen` keeps the old code and keeps changing it. A merge from `NextGen` can bring removed code back in two ways:

- **With a conflict.** NextGen edits something `refactor` deleted, and git stops. Whether it stays deleted then depends on whoever resolves the conflict.
- **Without one.** Nothing conflicts, but the merged code is broken or brings an old name back:
  - NextGen code uses an import that `refactor` removed as unused, which raises a `NameError` when it runs;
  - it imports or calls a function `refactor` deleted;
  - it uses a name `refactor` renamed.

  The rest of the suite misses these whenever no test runs that code path.

`src/anime/tests/test_refactor_ledger.py` catches both. It runs in `run_batches` like every other test, so it runs after every sync. It reads source files and imports nothing.

## What it checks

| Check | Catches |
|---|---|
| Removed names stay removed | A function, class, constant or method in `removed.json` defined again, or a removed method still called. |
| Renamed names stay renamed | The old name of a `renamed` entry defined again in its module. |
| Text rules | `only_in`: a pattern found outside the files listed (one home for a definition). `not_in`: a pattern back in a file it was removed from. Tests are exempt from `only_in`. |
| Every name a module uses is defined | NextGen code using an import that was removed as unused, or a name that was deleted or renamed in the same module. |
| Every internal import resolves | `from src.anime... import X` and `import src.anime...` where the module or `X` no longer exists, including imports inside functions. |
| Module attributes resolve | `module.X` through an imported `src.anime` module where `X` no longer exists. |
| Patch targets resolve | `mock.patch('src.anime....X')` strings pointing at something that no longer exists. |

It scans `src/anime` (tests included), `fixes/` and the Python scripts in `bin/`.

Replaying the `1c785beb` sync with every conflict resolved in NextGen's favour, the test reported:
- `audio_tagged()` and `prowlarr_search._release()` defined again;
- `audio.py` using `_TAG`, which the restored `audio_tagged()` needs but which stayed deleted.

The rest of the suite would have missed that last one, because the tests of `audio_tagged()` were removed with it.

## The package name after a sync

On `refactor` the package is `src/anime` (owner decision, 2026-11-08); NextGen
keeps `src/nextgen`. Git's directory-rename detection puts NextGen's changed and
new files under `src/anime/`, but the lines NextGen adds still say
`src.nextgen`. After every sync merge, before running the tests:

    python -I docs/refactor/rename_package.py

It is safe to run again. The ledger rule `PKG` fails until it has run.

## When it fails after a sync

NextGen is the source of truth, so the test does not decide anything. It points at the item and its ledger entry. For each finding:

1. If NextGen really uses the item again, keep NextGen's behaviour. Restore the code (or the import), then remove or change its entry in `removed.json` and say why in the PR.
2. If nothing needs it, the merge brought it back by accident. Delete it again.

Record the decision in the sync PR and in the `BASELINE.md` table.

## Adding entries

Add an entry in the same PR that removes or renames something:

```json
{"id": "RF-008 P3", "kind": "function", "module": "src.anime.example", "name": "old_helper", "why": "..."}
```

| Section | Fields | Notes |
|---|---|---|
| `removed` | `id`, `kind`, `module`, `name`, `why` | `kind` is `function`, `class`, `constant`, `method` (name `Class.method`) or `module`. |
| `renamed` | `id`, `kind`, `module`, `old`, `new`, `why` | The new name must exist. |
| `text` | `id`, `rule`, `pattern`, `files`, `why` | `rule` is `only_in` or `not_in`; `pattern` is a regular expression. |

Unused imports are not listed one by one. The "every name is defined" check covers them.

Persisted renames (RF-006: reason codes, job kinds, tables, settings keys) will need a `text` rule per old name. Migrations and historical SQL keep the old names, because they rewrite old rows.

## Finding candidates

`docs/refactor/audit_scan.py` is the scanner the audits use: `python -I docs/refactor/audit_scan.py . defs` (or `imports`, `modules`). Its hits are candidates, not proof; see `DEAD_CODE_AUDIT.md` for the method.
