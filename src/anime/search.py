"""One search for every dashboard page.

Each page asks for its own scope and gets back the same rows its normal list
returns; search only adds a WHERE clause to that list's existing query.

To add a scope: add an entry to SCOPES naming the text fields (and optional
id columns) to match, then pass ``match(...)`` into that page's list query.

Text the dashboard shows from a code (reason messages, job labels) is not in
the database, so ``codes`` maps a code column to that displayed text and a
word matches the codes whose text contains it.
"""
from dataclasses import dataclass
from functools import lru_cache

LIKE = "LIKE ? ESCAPE '\\'"
MAX_LENGTH = 100
MAX_WORDS = 8


class SearchInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    # SQL expressions containing ``{like}``; any one matching satisfies a word.
    fields: tuple
    # Integer columns a bare number (or ``#number``) may match exactly.
    ids: tuple = ()
    # (code column, name of a function returning {code: displayed text}).
    codes: tuple = ()


@lru_cache(maxsize=None)
def reason_texts():
    """Activity details: each reason's message and suggested action."""
    from src.anime.messages import ENGLISH
    from src.anime.reasons import REGISTRY
    return {code: ' '.join(filter(None, (ENGLISH.get(f'reason.{code}.message'),
                                          ENGLISH.get(f'reason.{code}.action'))))
            for code in REGISTRY}


TEXTS = {'reason': reason_texts}


SCOPES = {
    # Intake: one row per series (the series job).
    'intake': Scope(
        fields=(
            "series.title {like}",
            "EXISTS (SELECT 1 FROM recognized_aliases x WHERE x.anidb_id=series.anidb_id AND x.alias_key {like})",
            "EXISTS (SELECT 1 FROM anime_title_metadata t WHERE t.anidb_id=series.anidb_id "
            "AND (t.english_title {like} OR t.romaji_title {like}))",
        ),
        ids=('series.anidb_id',)),
    'recognized': Scope(
        fields=(
            "s.display_title {like}",
            "EXISTS (SELECT 1 FROM recognized_aliases x WHERE x.anidb_id=s.anidb_id AND x.alias_key {like})",
            "EXISTS (SELECT 1 FROM anime_title_metadata t WHERE t.anidb_id=s.anidb_id "
            "AND (t.english_title {like} OR t.romaji_title {like}))",
        ),
        ids=('s.anidb_id', 's.tvdb_series_id')),
}


def words(text):
    """Split a search box value into words; every word must match."""
    if type(text) is not str:
        raise SearchInvalid()
    text = text.strip()
    if not 2 <= len(text) <= MAX_LENGTH:
        raise SearchInvalid()
    found = text.split()
    if len(found) > MAX_WORDS:
        raise SearchInvalid()
    return found


def _pattern(word):
    escaped = word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return '%' + escaped + '%'


def match(scope, terms):
    """Return ``(sql, params)`` that is true when every word matches the scope."""
    if scope not in SCOPES:
        raise SearchInvalid()
    definition = SCOPES[scope]
    clauses, params = [], []
    for word in terms:
        options = []
        number = word[1:] if word.startswith('#') else word
        exact_id = number.isascii() and number.isdigit() and len(number) <= 18
        if exact_id:
            for column in definition.ids:
                options.append(column + '=?')
                params.append(int(number))
        if not (word.startswith('#') and exact_id and definition.ids):
            pattern = _pattern(word)
            for field in definition.fields:
                options.append(field.format(like=LIKE))
                params.extend([pattern] * field.count('{like}'))
            lowered = word.casefold()
            for column, texts in definition.codes:
                found = sorted(code for code, text in TEXTS[texts]().items()
                               if lowered in text.casefold())
                if found:
                    options.append(column + ' IN (' + ','.join('?' * len(found)) + ')')
                    params.extend(found)
        clauses.append('(' + ' OR '.join(options) + ')')
    return ' AND '.join(clauses), params
