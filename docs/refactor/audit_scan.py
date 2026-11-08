"""Dead-code reference scan used by the refactor audits (RF-002 and later).

    python -I docs/refactor/audit_scan.py <repository root> [defs|imports|modules]

defs     top-level functions, classes, constants and public methods that nothing
         else references (UNREF) or that only tests reference (TESTONLY)
imports  imported names a file never uses (UNUSED), or that another module
         imports from it (REEXPORT)
modules  modules nothing else references

Pass the repository root, not src/anime. References are counted in NextGen,
its tests, fixes/, bin/, deploy/, dashboard JS and SQL. A hit is a candidate,
not proof: check git history and dynamic uses before removing anything, and
record each removal in docs/refactor/removed.json (see LEDGER.md).
"""
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1])
src = root / "src" / "anime"
prod_files = [p for p in src.rglob("*.py") if "/tests/" not in str(p) and "__pycache__" not in str(p)]
test_files = [p for p in (src / "tests").rglob("*.py")]
other_files = [p for p in list((root / "bin").rglob("*")) + list((root / "deploy").rglob("*")) +
               list((root / "fixes").rglob("*.py")) +
               list(src.rglob("*.js")) + list(src.rglob("*.cjs")) + list(src.rglob("*.sql")) +
               list(src.rglob("*.html")) if p.is_file() and "__pycache__" not in str(p)]

texts = {}
for p in prod_files + test_files + other_files:
    try:
        texts[p] = p.read_text(errors="replace")
    except Exception:
        pass

defs = []  # (file, name, kind, lineno)
for p in prod_files:
    tree = ast.parse(texts[p])
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append((p, node.name, "func", node.lineno))
        elif isinstance(node, ast.ClassDef):
            defs.append((p, node.name, "class", node.lineno))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("__"):
                    defs.append((p, f"{node.name}.{sub.name}", "method", sub.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defs.append((p, t.id, "const", node.lineno))

def count(name, files):
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    return sum(len(pat.findall(texts[f])) for f in files if f in texts)

mode = sys.argv[2] if len(sys.argv) > 2 else "defs"
if mode == "defs":
    for p, name, kind, line in defs:
        short = name.split(".")[-1]
        if kind == "method" and (short.startswith("_") or len(short) < 4):
            continue
        own = count(short, [p])
        other_prod = count(short, [f for f in prod_files if f != p] + other_files)
        tests = count(short, test_files)
        if kind == "method":
            # methods: only flag when name appears exactly once anywhere
            if own + other_prod + tests == 1:
                print(f"UNREF\t{kind}\t{p.relative_to(root)}:{line}\t{name}")
            continue
        if own == 1 and other_prod == 0:
            tag = "TESTONLY" if tests else "UNREF"
            print(f"{tag}\t{kind}\t{p.relative_to(root)}:{line}\t{name}\ttests={tests}")
elif mode == "imports":
    for p in prod_files:
        tree = ast.parse(texts[p])
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported[a.asname or a.name] = node.lineno
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
               {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        allnames = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                allnames = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
        strtext = texts[p]
        for name, line in imported.items():
            if name in used or name in allnames or name == "*":
                continue
            # re-export check: someone imports name from this module
            mod = ".".join(p.relative_to(root).with_suffix("").parts)
            if p.name == "__init__.py":
                mod = mod[: -len(".__init__")]
            reexp = any(re.search(rf"from\s+[\w.]*{re.escape(mod.split('.')[-1])}\s+import[^\n]*\b{re.escape(name)}\b", texts[f])
                        for f in prod_files + test_files if f != p)
            print(f"{'REEXPORT' if reexp else 'UNUSED'}\t{p.relative_to(root)}:{line}\t{name}")
elif mode == "modules":
    for p in prod_files:
        if p.name in ("__init__.py", "__main__.py"):
            continue
        stem = p.stem
        refs = [f for f in prod_files + other_files if f != p and re.search(rf"\b{re.escape(stem)}\b", texts.get(f, ""))]
        trefs = [f for f in test_files if re.search(rf"\b{re.escape(stem)}\b", texts[f])]
        if not refs:
            print(f"{'TESTONLY' if trefs else 'UNREF'}\t{p.relative_to(root)}\ttests={len(trefs)}")
