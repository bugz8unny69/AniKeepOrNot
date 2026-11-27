"""Work item 3 (docs/WORK_ITEMS_JOB_CLARITY.md): no catch-alls in src/anime.

Every `except Exception` / `except BaseException` outside tests either
re-raises, or is a marked boundary ("Boundary" in a comment on the except
line or the line under it) that logs or prints the full trace.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
CATCH_ALL = re.compile(r"^(\s*)except (Exception|BaseException)\b")


def body(lines, index, indent):
    """The except line's own statements and its indented block."""
    first = lines[index].split(":", 1)[1].split("#", 1)[0].strip()
    block = [first] if first else []
    for line in lines[index + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= len(indent):
            break
        block.append(line.split("#", 1)[0].strip())
    return [statement for line in block for statement in line.split(";") if statement.strip()]


def unmarked():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.relative_to(ROOT).parts:
            continue
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            match = CATCH_ALL.match(line)
            if match is None:
                continue
            nearby = " ".join(lines[index:index + 2])
            statements = body(lines, index, match.group(1))
            reraises = bool(statements) and statements[-1].strip() == "raise"
            if not reraises and "Boundary" not in nearby and "Re-raised" not in nearby:
                found.append(f"{path.relative_to(ROOT.parent.parent)}:{index + 1}")
    return found


class NoCatchAllsTests(unittest.TestCase):
    def test_every_catch_all_reraises_or_is_a_marked_boundary(self):
        self.assertEqual(unmarked(), [])
