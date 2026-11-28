"""A module run as a script must define everything before it runs (production, 2026-11-28).

identity_process.py had `if __name__ == '__main__': sys.exit(main())` above the class its
code needs. Imported (as tests do), the whole module loads first, so nothing failed; run
as the API runs it (python -m), main() ran before the class existed: every new release
failed with NameError. The __main__ block must be each module's last statement.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main_blocks_with_code_after():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.relative_to(ROOT).parts:
            continue
        body = ast.parse(path.read_text()).body
        for index, node in enumerate(body):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and getattr(node.test.left, "id", "") == "__name__" and index != len(body) - 1):
                found.append(f"{path.relative_to(ROOT.parents[1])}:{node.lineno}")
    return found


class EntryPointTests(unittest.TestCase):
    def test_the_main_block_is_each_modules_last_statement(self):
        self.assertEqual(main_blocks_with_code_after(), [])
