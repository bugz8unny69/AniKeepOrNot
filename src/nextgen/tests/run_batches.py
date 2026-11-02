"""Run only fresh NextGen tests locally in small, independent batches.

Usage from repository root:
    python -m src.nextgen.tests.run_batches
    python -m src.nextgen.tests.run_batches --match schema
    python -m src.nextgen.tests.run_batches --batch-size 5
    python -m src.nextgen.tests.run_batches --list

No GitHub Actions, legacy imports, network calls or production services.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

TEST_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TEST_DIR.parents[2]
TEST_PACKAGE = "src.nextgen.tests"


def find_test_modules(match=None):
    """Discover only NextGen test files; legacy tests are intentionally excluded."""
    paths = sorted(TEST_DIR.glob("test_*.py"))
    if match:
        paths = [path for path in paths if match.lower() in path.stem.lower()]
    return [f"{TEST_PACKAGE}.{path.stem}" for path in paths]


def batch_modules(modules, size):
    """Yield deterministic chunks so one failed group does not skip remaining groups."""
    for start in range(0, len(modules), size):
        yield modules[start:start + size]


def main(argv=None):
    os.umask(0o022)
    parser = argparse.ArgumentParser(
        description="Run fresh NextGen unit tests locally, in named-file batches.")
    parser.add_argument("--match", default=None,
                        help="Only test files whose names contain this text (e.g. schema)")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="Number of test files per batch (default: 5)")
    parser.add_argument("--list", action="store_true", help="List selected modules only")
    args = parser.parse_args(argv)

    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    modules = find_test_modules(args.match)
    if not modules:
        print("No NextGen test files matched; refusing to report false success.",
              file=sys.stderr)
        return 2

    if args.list:
        print("\n".join(modules))
        return 0

    groups = list(batch_modules(modules, args.batch_size))
    failed = []
    for number, group in enumerate(groups, 1):
        print(f"\nNextGen test batch {number}/{len(groups)}: {', '.join(group)}",
              flush=True)
        status = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", *group],
            cwd=REPOSITORY_ROOT,
            check=False,
        ).returncode
        if status != 0:
            failed.append(number)

    print(f"\nNextGen batches: {len(groups)} total, "
          f"{len(groups) - len(failed)} passed, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
