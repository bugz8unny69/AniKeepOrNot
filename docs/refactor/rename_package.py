"""Rename the package src/nextgen -> src/anime (owner decision, 2026-11-08).

Run from the repository root:  python -I docs/refactor/rename_package.py

Safe to run again, and meant to run after every NextGen -> refactor sync:
NextGen keeps src/nextgen, so lines it adds still say src.nextgen even after
git's directory-rename detection puts its files under src/anime/. The ledger
rule "PKG" fails the tests until this script has run.

Not rewritten: SQL migrations (they describe history; 067 rewrites stored
paths in the database), this script, and any line containing the marker
"rename_package: keep" (text that must name the old package, such as test
fixtures of old History rows).
"""
import pathlib
import re
import subprocess

OLD, NEW = 'nextgen', 'anime'
SKIP = ('docs/refactor/rename_package.py',)
KEEP = 'rename_package: keep'
SKIP_PREFIX = ('src/anime/storage/sql/', 'src/nextgen/storage/sql/')

RULES = (
    (re.compile(r'\bsrc\.nextgen\b'), 'src.anime'),                       # imports, python -m, patch targets
    (re.compile(r'\bsrc/nextgen\b'), 'src/anime'),                        # paths, failed checks
    (re.compile(r'''(["'])nextgen\.(?=[a-z]|\1)'''), r'\1anime.'),      # logger names "nextgen.worker", "nextgen." + x
    (re.compile(r'''== "nextgen" or safe_record\.name\.startswith\("nextgen\."\)'''),
     '== "anime" or safe_record.name.startswith("anime.")'),
    (re.compile(r'''safe_record\.name\[len\("nextgen"\): \]'''), 'safe_record.name[len("anime"): ]'),
    (re.compile(r'''(["'])src\1 / (["'])nextgen\2'''), r'\1src\1 / \2anime\2'),        # ROOT / 'src' / 'nextgen'
    (re.compile(r'\bat nextgen/'), 'at anime/'),                         # error locations in log text
)


def tracked():
    out = subprocess.run(['git', 'ls-files', '-z'], capture_output=True, check=True).stdout
    return [name for name in out.decode().split('\0') if name]


def main():
    root = pathlib.Path('.')
    if (root / 'src/nextgen').is_dir():
        if (root / 'src/anime').exists():
            # After a sync: move any file git left under src/nextgen into src/anime.
            for path in sorted((root / 'src/nextgen').rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    target = root / 'src/anime' / path.relative_to(root / 'src/nextgen')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(['git', 'mv', '-f', str(path), str(target)], check=True)
        else:
            subprocess.run(['git', 'mv', 'src/nextgen', 'src/anime'], check=True)
    changed = 0
    for name in tracked():
        if name in SKIP or name.startswith(SKIP_PREFIX):
            continue
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        lines = []
        for line in text.splitlines(keepends=True):
            if KEEP not in line:
                for pattern, replacement in RULES:
                    line = pattern.sub(replacement, line)
            lines.append(line)
        new = ''.join(lines)
        if new != text:
            path.write_text(new)
            changed += 1
    print(f'{changed} files rewritten')


if __name__ == '__main__':
    main()
