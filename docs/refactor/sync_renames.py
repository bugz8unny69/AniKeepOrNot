"""Apply refactor's renames to the code a NextGen -> refactor sync brings in.

Run from the repository root while the sync merge is in progress (MERGE_HEAD
set), after resolving conflicts, before the tests:

    python -I docs/refactor/sync_renames.py          # files NextGen changed since the merge base
    python -I docs/refactor/sync_renames.py FILE...  # or exactly these files

NextGen keeps every old name. Each rename refactor made is re-applied to just
the files NextGen changed, so refactor's own deliberate uses of old names
(migrations, migration tests, the audit) are never touched:

* RF-005  code names: renamed modules and every "trial" identifier.
* RF-006a stored names: tables, column, indexes, job kinds, event source, API field.
* RF-006b reason and event codes renamed by the audit (reason_code_proposals.py).
* RF-006c per-service codes retired into SERVICE_* codes.
* RF-006d settings keys trial.* -> user_evaluation.*.
* Moved names: VerifiedEpisode now lives in episode_identity.

Lines containing "rename_package: keep" are left alone. Run
docs/refactor/rename_package.py first (the package name). The ledger and
reason-code audit tests then list anything this could not rename (a new code
NextGen added, for example), to decide by hand.
"""
import io
import pathlib
import re
import runpy
import subprocess
import sys
import tokenize

ROOT = pathlib.Path('.')
KEEP = 'rename_package: keep'
NEVER = ('src/anime/storage/sql/', 'src/anime/storage/MIGRATIONS.md', 'docs/refactor/')

MODULES = {
    'jobs/discover_sonarr_backfill_job': 'jobs/discover_episode_releases_job',
    'storage/sonarr_backfill': 'storage/episode_discovery',
    'storage/sonarr_candidates': 'storage/release_candidates',
    'storage/sonarr_selection': 'storage/release_selection',
    'storage/sonarr_dispatch': 'storage/release_dispatch',
    'storage/sonarr_cached_retry': 'storage/cached_release_retry',
    'storage/sonarr_magnet_intake': 'storage/magnet_intake',
    'trial_policy': 'user_evaluation_policy',
    'storage/trials': 'storage/user_evaluations',
    'storage/batch_trials': 'storage/batch_user_evaluations',
    'batch/durable_trial': 'batch/user_evaluation',
    'jobs/evaluate_trial_job': 'jobs/select_user_evaluation_job',
    'jobs/stage_trial_media_job': 'jobs/stage_user_evaluation_media_job',
    'tests/test_sonarr_magnet_intake': 'tests/test_magnet_intake',
    'tests/test_sonarr_magnet_cancellation': 'tests/test_magnet_intake_cancellation',
    'tests/test_trial_policy': 'tests/test_user_evaluation_policy',
    'tests/test_trial_migration': 'tests/test_user_evaluation_migration',
    'tests/test_batch_trial_job': 'tests/test_batch_user_evaluation_job',
}
STEMS = {pathlib.Path(o).name: pathlib.Path(n).name for o, n in MODULES.items()}
DOTTED = {'src.anime.' + o.replace('/', '.'): 'src.anime.' + n.replace('/', '.') for o, n in MODULES.items()}
TRIAL = [('evaluate_trial', 'select_user_evaluation'), ('durable_trial', 'user_evaluation'),
         ('trial_evaluation', 'user_evaluation_check'), ('TrialEvaluation', 'UserEvaluationCheck'),
         ('TRIAL_EVALUATION', 'USER_EVALUATION_CHECK'),
         ('TRIAL', 'USER_EVALUATION'), ('Trial', 'UserEvaluation'), ('trial', 'user_evaluation')]
STORED = {
    'DISCOVER_SONARR_BACKFILL': 'DISCOVER_EPISODE_RELEASES', 'EVALUATE_TRIAL': 'SELECT_USER_EVALUATION',
    'STAGE_TRIAL_MEDIA': 'STAGE_USER_EVALUATION_MEDIA', 'sonarr-discovery': 'release-discovery',
    'sonarr_backfill_results': 'episode_discovery_results', 'sonarr_episode_candidates': 'episode_release_candidates',
    'sonarr_episode_selections': 'episode_release_selections', 'sonarr_magnet_intents': 'magnet_intents',
    'trial_plans': 'user_evaluation_plans', 'trial_evaluations': 'user_evaluation_checks',
    'batch_trial_plans': 'batch_user_evaluation_plans', 'batch_trial_files': 'batch_user_evaluation_files',
    'trial_plan_id': 'user_evaluation_plan_id', 'idx_trial_evaluations_plan': 'idx_user_evaluation_checks_plan',
    'idx_batch_trial_by_episode': 'idx_batch_user_evaluation_by_episode', 'trial_plan_ready': 'user_evaluation_plan_ready',
}


def _codes():
    d = runpy.run_path(str(ROOT / 'docs/refactor/reason_code_proposals.py'))
    codes = {k: v[0] for table in (d['REASONS'], d['EVENTS']) for k, v in table.items() if k != v[0]}
    codes.update({k: v for k, v in d['REMOVED'].items() if type(v) is str})
    codes.update({k: v[0] for k, v in d['RETIRED'].items()})
    return codes


def _settings():
    from_schema = (ROOT / 'src/anime/configuration_schema.py').read_text()
    pairs = re.findall(r'"(user_evaluation\.[a-z_]+)": "(trial\.[a-z_]+)"', from_schema)
    return {old: new for new, old in pairs}


WORDS = {**STORED, **_codes(), **_settings()}
WORD = re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(re.escape(k) for k in sorted(WORDS, key=len, reverse=True))
                  + r')(?![A-Za-z0-9_])')
MOVED = [(re.compile(r'from src\.anime\.sonarr_episode import ([^\n]*\bVerifiedEpisode\b[^\n]*)'), 'VerifiedEpisode',
          'src.anime.episode_identity', 'src.anime.sonarr_episode')]


def _name(token):
    if token in STEMS:
        return STEMS[token]
    if not re.search('trial', token, re.I):
        return token
    for old, new in TRIAL:
        token = token.replace(old, new)
    return token


def _string(text):
    for old in sorted(DOTTED, key=len, reverse=True):
        text = re.sub(re.escape(old) + r'(?![A-Za-z0-9_])', DOTTED[old], text)
    return WORD.sub(lambda m: WORDS[m.group(1)], text)


def _moved(text):
    for pattern, name, new_module, old_module in MOVED:
        def split(m):
            names = [n.strip() for n in m.group(1).split(',')]
            rest = [n for n in names if n != name]
            line = f'from {new_module} import {name}'
            return line + (f'\nfrom {old_module} import {", ".join(rest)}' if rest else '')
        text = pattern.sub(split, text)
    return text


def python(source):
    keep = {n for n, line in enumerate(source.splitlines(), 1) if KEEP in line}
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    edits = []
    strings = {tokenize.STRING, getattr(tokenize, 'FSTRING_MIDDLE', -1)}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.start[0] in keep:
            continue
        new = (_name(tok.string) if tok.type == tokenize.NAME else
               _string(tok.string) if tok.type in strings else tok.string)
        if new != tok.string:
            start = offsets[tok.start[0] - 1] + tok.start[1]
            edits.append((start, offsets[tok.end[0] - 1] + tok.end[1], new))
    for start, end, new in reversed(edits):
        source = source[:start] + new + source[end:]
    return _moved(source)


def text(source):
    return ''.join(line if KEEP in line else _string(line) for line in source.splitlines(keepends=True))


def changed_by_nextgen():
    base = subprocess.run(['git', 'merge-base', 'HEAD', 'MERGE_HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    names = subprocess.run(['git', 'diff', '--name-only', '--diff-filter=AMR', base, 'MERGE_HEAD'],
                           capture_output=True, text=True, check=True).stdout.split()
    return [n.replace('src/nextgen/', 'src/anime/', 1) for n in names]


def main(argv):
    files = argv[1:] or changed_by_nextgen()
    changed = []
    for name in files:
        path = ROOT / name
        for old, new in MODULES.items():                     # a NextGen file under an old module name
            if name == f'src/anime/{old}.py':
                path = ROOT / f'src/anime/{new}.py'
        if name.startswith(NEVER) or not path.is_file():
            continue
        if re.search(r'tests/test_[a-z0-9_]*(migration|readmission)[a-z0-9_]*\.py$', path.as_posix()):
            # These build old database shapes with the old names on purpose.
            print('   check by hand (old schema on purpose):', path.as_posix())
            continue
        source = path.read_text()
        new = python(source) if path.suffix == '.py' or name.startswith('bin/') else text(source)
        if new != source:
            path.write_text(new)
            changed.append(path.as_posix())
    print(f'{len(changed)} files renamed')
    for name in changed:
        print('  ', name)


if __name__ == '__main__':
    main(sys.argv)
