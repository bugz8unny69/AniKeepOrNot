"""Static checks that dashboard.js only uses what exists (owner, 2026-11-27).

The series job removed the Jobs page, its elements and its API routes, but the script kept
code wired to them: each reload found the next missing element. These read the script and
fail on the whole list at once:

1. every element ID the script looks up is in index.html;
2. every API path the script requests is a route the API serves.

Not covered here: what the API returns and how the pages draw it. A browser test of every
page against the real API with a test database is the next step if gaps show up
(docs/DASHBOARD_BROWSER_TEST.md).
"""
from pathlib import Path
import re
import unittest

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"
APP = Path(__file__).resolve().parents[1] / "api" / "app.py"
SCRIPT = (STATIC / "dashboard.js").read_text()
PAGE = (STATIC / "index.html").read_text()

# Lookups whose ID is computed: each is checked against the IDs it can take.
COMPUTED = {
    "id": None,                                                   # The loop and map literals below.
}


def page_ids():
    return set(re.findall(r'id="([^"]+)"', PAGE))


def looked_up_ids():
    """Literal $('x') lookups, and the ID lists the computed lookups iterate."""
    found = set(re.findall(r"\$\('([^']+)'\)", SCRIPT))
    for listed in re.findall(r"for \(const id of \[([^\]]+)\]\)", SCRIPT):
        found |= set(re.findall(r"'([^']+)'", listed))
    for listed in re.findall(r"const id = \{([^}]+)\}\[state\.page\]", SCRIPT):
        found |= set(re.findall(r":\s*'([^']+)'", listed))
    for prefix, names in re.findall(r"for \(const name of \[([^\]]+)\]\) \$\('([\w-]+)' \+ name\)", SCRIPT):
        found |= {names + n for n in re.findall(r"'([^']+)'", prefix)}
    return found


def computed_lookups():
    """$(expr) whose argument is not a string literal."""
    return sorted({m.strip() for m in re.findall(r"\$\(([^')][^)]*)\)", SCRIPT)})


HOLE = "\x00"


def requested_paths():
    """API paths the script requests; each computed part is a HOLE that any value may fill."""
    paths = set()
    for body in re.findall(r"`\$\{API\}(/[^`]*)`", SCRIPT):              # `${API}/notifications/${id}/actions/${a}`
        paths.add(re.sub(r"\$\{[^}]+\}", HOLE, body))
    for m in re.finditer(r"API \+ ('(/[^']*)'(?:\s*\+\s*[\w.\[\]()]+(?:\s*\+\s*'[^']*')?)*)", SCRIPT):
        parts = re.findall(r"'([^']*)'|\+\s*([\w.\[\]()]+)", m.group(1))
        # A variable named query is the query string (?a=b), not part of the path.
        paths.add("".join(text if text or not name else ("" if name.rstrip(")") == "query" else HOLE) for text, name in parts))
    return {p.split("?", 1)[0] for p in paths if p != "/"}


def fillings(path, words):
    """Every way to fill the HOLEs in path with the given words."""
    from itertools import product
    pieces = path.split(HOLE)
    for chosen in product(words, repeat=len(pieces) - 1):
        yield "".join(piece + (chosen[i] if i < len(chosen) else "") for i, piece in enumerate(pieces))


def api_routes():
    """(exact, patterns): the paths the API dispatches, from app.py."""
    source = APP.read_text()
    exact = set(re.findall(r"PREFIX\+'(/[^']*)'", source))
    patterns = [re.compile(p) for p in re.findall(r"PREFIX\+r'([^']*)'", source)]
    return exact, patterns


class DashboardStaticTests(unittest.TestCase):
    def test_every_element_the_script_looks_up_is_on_the_page(self):
        missing = sorted(looked_up_ids() - page_ids())
        self.assertEqual(missing, [], "dashboard.js looks up elements index.html does not have")

    def test_computed_lookups_are_known(self):
        # A new computed $(...) must have its possible IDs checked above.
        self.assertEqual(computed_lookups(), ["id"])

    def test_every_api_path_the_script_requests_is_served(self):
        exact, patterns = api_routes()
        # A computed part may be a number, a word, or any word a route lists, e.g. (tvdb|sonarr).
        words = {"1", "word"} | {w for p in patterns for group in re.findall(r"\(([a-z_|]+)\)", p.pattern)
                                 for w in group.split("|")}
        unknown = sorted(p.replace(HOLE, "{…}") for p in requested_paths()
                         if not any(f in exact or any(pattern.fullmatch(f) for pattern in patterns)
                                    for f in fillings(p, sorted(words))))
        self.assertEqual(unknown, [], "dashboard.js requests API paths app.py does not serve")

    def test_the_checks_find_what_they_check(self):
        paths = requested_paths()
        self.assertIn("/series/" + HOLE + "/keep", paths)             # Concatenated.
        self.assertIn("/notifications/" + HOLE + "/actions/" + HOLE, paths)   # Template literal.
        self.assertIn("/notifications", paths)                        # + query: the query string.
        self.assertIn("intake-search", looked_up_ids())
        self.assertGreater(len(page_ids()), 50)
