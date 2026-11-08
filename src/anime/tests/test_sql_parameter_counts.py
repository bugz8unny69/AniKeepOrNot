"""Static guard: literal SQL placeholders must match literal parameter tuples.

A mismatch is a deterministic sqlite3.ProgrammingError at runtime, for example
the batch transfer selection INSERT that once bound 8 values to 9 placeholders.
Only statically checkable calls are inspected: a literal (or literal-concatenated)
SQL string passed with a literal tuple or list and no starred arguments.
"""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1]


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        return None if left is None or right is None else left + right
    return None


def mismatches():
    found, checked = [], 0
    for path in sorted(SOURCE.rglob('*.py')):
        if 'tests' in path.relative_to(SOURCE).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('execute', 'executemany') and len(node.args) >= 2):
                continue
            sql, params = _literal(node.args[0]), node.args[1]
            if (sql is None or not isinstance(params, (ast.Tuple, ast.List))
                    or any(isinstance(item, ast.Starred) for item in params.elts)):
                continue
            checked += 1
            if sql.count('?') != len(params.elts):
                found.append(f'{path.relative_to(SOURCE)}:{node.lineno} '
                             f'placeholders={sql.count("?")} values={len(params.elts)}')
    return found, checked


class SqlParameterCountTests(unittest.TestCase):
    def test_literal_statements_bind_every_placeholder(self):
        found, checked = mismatches()
        self.assertGreater(checked, 100, 'scanner unexpectedly found few statements')
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
