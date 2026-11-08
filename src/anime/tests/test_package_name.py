"""The package is src/anime on refactor; nothing may still point at src/nextgen."""  # rename_package: keep
import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD = re.compile(r'\bsrc[./]next' + r'gen\b')
KEEP = 'rename_package: keep'


class PackageNameTests(unittest.TestCase):
    def test_every_logger_name_is_under_anime(self):
        # A logger outside "anime." is never configured: its lines are lost.
        wrong = []
        for path in ROOT.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and node.args):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                if name not in ('getLogger', 'Logger'):
                    continue
                first = node.args[0]
                if isinstance(first, ast.BinOp) and isinstance(first.left, ast.Constant):
                    first = first.left
                if (isinstance(first, ast.Constant) and type(first.value) is str
                        and first.value.split('.')[0] in ('next' + 'gen',)):
                    wrong.append(f'{path.relative_to(ROOT)}:{node.lineno}: {first.value!r}')
        self.assertEqual(wrong, [])

    def test_tests_name_the_old_package_only_on_marked_lines(self):
        # The ledger rule PKG exempts tests; fixtures of old data must be marked
        # so docs/refactor/rename_package.py leaves them alone.
        found = [f'{path.relative_to(ROOT)}:{number}: {line.strip()}'
                 for path in (ROOT / 'tests').rglob('*.py')
                 for number, line in enumerate(path.read_text().splitlines(), 1)
                 if OLD.search(line) and KEEP not in line]
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
