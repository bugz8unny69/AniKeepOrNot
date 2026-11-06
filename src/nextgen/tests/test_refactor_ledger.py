"""Merges from NextGen must not quietly bring back what the refactor removed.

Static checks only: no module is imported. See docs/refactor/LEDGER.md.

1. Everything in docs/refactor/removed.json stays removed or renamed.
2. Every name a module uses is defined (catches NextGen code that uses an
   import the refactor deleted as unused).
3. Every src.nextgen import, module attribute and mock.patch target resolves
   (catches NextGen code calling something the refactor removed).
"""
import ast
import builtins
import json
from pathlib import Path
import re
import symtable
import unittest

ROOT = Path(__file__).resolve().parents[3]
LEDGER = ROOT / 'docs' / 'refactor' / 'removed.json'
PACKAGE = ROOT / 'src' / 'nextgen'

DECIDE = ('NextGen is the source of truth. If NextGen really uses this again, keep '
          "NextGen's behaviour: restore it, and remove or change its entry in "
          'docs/refactor/removed.json with the reason. If nothing needs it, remove it again.')

_MODULE_DUNDERS = {'__file__', '__name__', '__doc__', '__spec__', '__loader__', '__package__',
                   '__path__', '__builtins__', '__annotations__', '__dict__', '__cached__'}
_BUILTINS = set(dir(builtins)) | _MODULE_DUNDERS


def _python_files():
    """NextGen (with its tests), fixes/ and the Python scripts in bin/."""
    files = [p for p in PACKAGE.rglob('*.py') if '__pycache__' not in p.parts]
    files += [p for p in (ROOT / 'fixes').rglob('*.py') if '__pycache__' not in p.parts]
    for path in sorted((ROOT / 'bin').iterdir()):
        if path.is_file() and path.read_bytes()[:64].startswith(b'#!') and b'python' in path.read_bytes()[:64]:
            files.append(path)
    return sorted(files)


def _production(path):
    return 'tests' not in path.relative_to(ROOT).parts


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def _module_index():
    index = {}
    for path in PACKAGE.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        parts = list(path.relative_to(ROOT).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        index['.'.join(parts)] = path
    for folder in [PACKAGE, *PACKAGE.rglob('*')]:   # Packages without __init__.py too.
        if folder.is_dir() and folder.name != '__pycache__':
            index.setdefault('.'.join(folder.relative_to(ROOT).parts), None)
    index.setdefault('src', None)
    return index


def _tables(table):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def _kind(table):
    kind = table.get_type()
    return getattr(kind, 'value', kind)


class _Source:
    def __init__(self, path, text=None):
        self.path = path
        self.text = path.read_text() if text is None else text
        self.tree = ast.parse(self.text, str(path))
        self.symbols = symtable.symtable(self.text, str(path), 'exec')
        names = set()
        for table in _tables(self.symbols):
            module = _kind(table) == 'module'
            for sym in table.get_symbols():
                if (module and (sym.is_assigned() or sym.is_imported() or sym.is_namespace())) \
                        or (sym.is_declared_global() and sym.is_assigned()):
                    names.add(sym.get_name())
        self.globals = names

    def undefined(self):
        found = set()
        for table in _tables(self.symbols):
            module = _kind(table) == 'module'
            for sym in table.get_symbols():
                if not sym.is_referenced():
                    continue
                name = sym.get_name()
                if module:
                    unbound = not (sym.is_assigned() or sym.is_imported() or sym.is_namespace())
                else:
                    unbound = sym.is_global()
                if unbound and name not in self.globals and name not in _BUILTINS:
                    found.add(name)
        return found

    def shadowed(self):
        """Names bound anywhere other than by a module-level import."""
        names = set()
        for table in _tables(self.symbols):
            module = _kind(table) == 'module'
            for sym in table.get_symbols():
                if module:
                    if sym.is_assigned() or sym.is_namespace():
                        names.add(sym.get_name())
                elif sym.is_local() or sym.is_parameter():
                    names.add(sym.get_name())
        return names


class _Repository:
    def __init__(self):
        self.index = _module_index()
        self.files = _python_files()
        self.sources = {}
        self._exports = {}

    def source(self, path):
        if path not in self.sources:
            self.sources[path] = _Source(path)
        return self.sources[path]

    def exports(self, module):
        if module not in self._exports:
            path = self.index.get(module)
            names = set() if path is None else set(self.source(path).globals)
            prefix = module + '.'
            names |= {m[len(prefix):].split('.')[0] for m in self.index if m.startswith(prefix)}
            self._exports[module] = names
        return self._exports[module]

    def module_for(self, path, node):
        if not node.level:
            return node.module
        parts = list(path.relative_to(ROOT).with_suffix('').parts)
        base = parts[:-1] if parts[-1] != '__init__' else parts[:-1]
        base = base[:len(base) - (node.level - 1)]
        return '.'.join(base + ([node.module] if node.module else []))

    def definitions(self, module):
        """Top-level names and Class.method names defined in a module."""
        path = self.index.get(module)
        if path is None:
            return set()
        names = set()
        for node in self.source(path).tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                names.add(node.name)
                names |= {f'{node.name}.{sub.name}' for sub in node.body
                          if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))}
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
        return names

    def resolve(self, dotted):
        """Return a problem with a dotted src.nextgen path, or None."""
        parts = dotted.split('.')
        for end in range(len(parts), 0, -1):
            module = '.'.join(parts[:end])
            if module in self.index:
                if end == len(parts):
                    return None
                if parts[end] in self.exports(module):
                    return None
                return f'{module} has no {parts[end]!r}'
        return f'no module {dotted}'


REPO = _Repository()


def _ledger():
    return json.loads(LEDGER.read_text())


class LedgerTests(unittest.TestCase):
    def test_ledger_entries_are_complete(self):
        ledger = _ledger()
        for entry in ledger['removed']:
            with self.subTest(entry=entry['id']):
                self.assertEqual(set(entry), {'id', 'kind', 'module', 'name', 'why'})
                self.assertIn(entry['kind'], {'function', 'method', 'class', 'constant', 'module'})
                if entry['kind'] != 'module':
                    self.assertIn(entry['module'], REPO.index, 'the module of a removed name must exist')
        for entry in ledger['renamed']:
            with self.subTest(entry=entry['id']):
                self.assertEqual(set(entry), {'id', 'kind', 'module', 'old', 'new', 'why'})
                self.assertIn(entry['new'], REPO.definitions(entry['module']),
                              f"{entry['id']}: the new name {entry['new']} is not defined in {entry['module']}")
        for entry in ledger['text']:
            with self.subTest(entry=entry['id']):
                self.assertEqual(set(entry), {'id', 'rule', 'pattern', 'files', 'why'})
                self.assertIn(entry['rule'], {'only_in', 'not_in'})
                re.compile(entry['pattern'])
                for name in entry['files']:
                    self.assertTrue((ROOT / name).is_file(), name)

    def test_removed_things_stay_removed(self):
        problems = []
        for entry in _ledger()['removed']:
            if entry['kind'] == 'module':
                if entry['module'] in REPO.index:
                    problems.append(f"{entry['id']}: module {entry['module']} is back. {entry['why']}")
                continue
            if entry['name'] in REPO.definitions(entry['module']):
                problems.append(f"{entry['id']}: {entry['module']}.{entry['name']} is defined again. {entry['why']}")
            if entry['kind'] == 'method':
                method = entry['name'].split('.')[-1]
                for path in REPO.files:
                    for node in ast.walk(REPO.source(path).tree):
                        if isinstance(node, ast.Attribute) and node.attr == method:
                            problems.append(f"{entry['id']}: {_rel(path)}:{node.lineno} calls .{method}(). {entry['why']}")
        for entry in _ledger()['renamed']:
            if entry['old'] in REPO.definitions(entry['module']):
                problems.append(f"{entry['id']}: {entry['module']}.{entry['old']} is defined again; "
                                f"it was renamed to {entry['new']}. {entry['why']}")
        self.assertEqual(problems, [], '\n'.join(problems + [DECIDE]))

    def test_text_rules(self):
        problems = []
        for entry in _ledger()['text']:
            pattern = re.compile(entry['pattern'])
            listed = set(entry['files'])
            if entry['rule'] == 'only_in':
                checked = [p for p in REPO.files if _production(p) and _rel(p) not in listed]
            else:
                checked = [ROOT / name for name in listed]
            for path in checked:
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if pattern.search(line):
                        problems.append(f"{entry['id']}: {_rel(path)}:{number}: {line.strip()}  ({entry['why']})")
        self.assertEqual(problems, [], '\n'.join(problems + [DECIDE]))


class ResolutionTests(unittest.TestCase):
    def test_every_name_a_module_uses_is_defined(self):
        problems = []
        for path in REPO.files:
            for name in sorted(REPO.source(path).undefined()):
                problems.append(f'{_rel(path)}: {name!r} is used but never defined or imported')
        self.assertEqual(problems, [], '\n'.join(problems + [
            'A NextGen change may use an import the refactor removed as unused. Add the import back.']))

    def test_every_internal_import_resolves(self):
        problems = []
        for path in REPO.files:
            for node in ast.walk(REPO.source(path).tree):
                if isinstance(node, ast.ImportFrom):
                    module = REPO.module_for(path, node)
                    if not module or not module.startswith('src.'):
                        continue
                    if module not in REPO.index:
                        problems.append(f'{_rel(path)}:{node.lineno}: no module {module}')
                        continue
                    for alias in node.names:
                        if alias.name not in REPO.exports(module):
                            problems.append(f'{_rel(path)}:{node.lineno}: {module} has no {alias.name!r}')
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('src.') and alias.name not in REPO.index:
                            problems.append(f'{_rel(path)}:{node.lineno}: no module {alias.name}')
        self.assertEqual(problems, [], '\n'.join(problems + [DECIDE]))

    def test_module_attributes_resolve(self):
        problems = []
        for path in REPO.files:
            source = REPO.source(path)
            aliases = {}
            for node in source.tree.body:
                if isinstance(node, ast.ImportFrom):
                    module = REPO.module_for(path, node)
                    for alias in node.names:
                        target = f'{module}.{alias.name}'
                        if module and target in REPO.index:
                            aliases[alias.asname or alias.name] = target
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname and alias.name in REPO.index:
                            aliases[alias.asname] = alias.name
            for name in source.shadowed():
                aliases.pop(name, None)
            for node in ast.walk(source.tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in aliases):
                    problem = REPO.resolve(f'{aliases[node.value.id]}.{node.attr}')
                    if problem:
                        problems.append(f'{_rel(path)}:{node.lineno}: {problem}')
        self.assertEqual(problems, [], '\n'.join(problems + [DECIDE]))

    def test_patch_targets_resolve(self):
        target = re.compile(r'src\.nextgen(?:\.\w+)+')
        problems = []
        for path in REPO.files:
            for node in ast.walk(REPO.source(path).tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and target.fullmatch(node.value):
                    problem = REPO.resolve(node.value)
                    if problem:
                        problems.append(f'{_rel(path)}:{node.lineno}: {node.value!r}: {problem}')
        self.assertEqual(problems, [], '\n'.join(problems + [DECIDE]))


class CheckerTests(unittest.TestCase):
    """The checks themselves find what they are meant to find."""

    def source(self, text):
        return _Source(ROOT / 'src' / 'nextgen' / '_ledger_probe.py', text)

    def test_a_removed_import_still_used_is_found(self):
        probe = self.source('def run():\n    return asyncio.sleep(0)\n')
        self.assertEqual(probe.undefined(), {'asyncio'})

    def test_defined_names_are_not_reported(self):
        probe = self.source(
            'import asyncio\nfrom os import path as p\nX = 1\n'
            'class C:\n    Y = X\n    def m(self):\n        return self, C, Y_local\n'
            'Y_local = [n for n in range(3)]\n'
            'def run(a, *b, **c):\n    global Z\n    Z = 2\n    for i in a: pass\n'
            '    try: pass\n    except ValueError as e: return e, i, b, c, p, asyncio, len\n')
        self.assertEqual(probe.undefined(), set())

    def test_removed_names_are_reported_by_resolution(self):
        self.assertIsNone(REPO.resolve('src.nextgen.acquisition.audio.dual_audio_tagged'))
        missing = '.'.join(('src.nextgen.acquisition.audio', 'no_such_name'))  # Not a literal: the scan reads this file.
        self.assertEqual(REPO.resolve(missing), "src.nextgen.acquisition.audio has no 'no_such_name'")
        self.assertIsNone(REPO.resolve('src.nextgen.storage.batch_candidates'))
        self.assertIn('no module', REPO.resolve('srcx.missing'))

    def test_definitions_include_functions_and_methods(self):
        defined = REPO.definitions('src.nextgen.sonarr')
        self.assertIn('Client', defined)
        self.assertTrue(any(name.startswith('Client.') for name in defined))
        self.assertNotIn('Client.add_payload', defined)
        self.assertIn('dual_audio_tagged', REPO.definitions('src.nextgen.acquisition.audio'))


if __name__ == '__main__':
    unittest.main()
