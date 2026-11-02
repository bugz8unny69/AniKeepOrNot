"""One search for every dashboard page.

Each page asks for its own scope and gets back the same rows its normal list
returns; search only adds a WHERE clause to that list's existing query.

To add a scope: add an entry to SCOPES naming the text fields (and optional
id columns) to match, then pass ``match(...)`` into that page's list query.
"""
from dataclasses import dataclass

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


SCOPES = {
    'jobs': Scope(
        fields=(
            "(SELECT CASE WHEN json_valid(CAST(e.raw_body AS TEXT)) "
            "THEN json_extract(CAST(e.raw_body AS TEXT),'$.release_name') END "
            "FROM incoming_events e WHERE e.event_id=jobs.event_id) {like}",
            "EXISTS (SELECT 1 FROM identity_results r JOIN identified_anime a USING(anidb_id) "
            "WHERE r.event_id=jobs.event_id AND a.title {like})",
            "EXISTS (SELECT 1 FROM identity_results r JOIN anime_title_metadata t USING(anidb_id) "
            "WHERE r.event_id=jobs.event_id AND (t.english_title {like} OR t.romaji_title {like}))",
        ),
        ids=('jobs.job_id',)),
    'intake': Scope(
        fields=(
            "identified_anime.title {like}",
            "EXISTS (SELECT 1 FROM recognized_aliases x "
            "WHERE x.anidb_id=identified_anime.anidb_id AND x.alias_key {like})",
            "EXISTS (SELECT 1 FROM identity_results r WHERE r.anidb_id=identified_anime.anidb_id "
            "AND (json_extract(r.series_json,'$.anime.english_title') {like} "
            "OR json_extract(r.series_json,'$.anime.original_title') {like}))",
            "EXISTS (SELECT 1 FROM anime_title_metadata t WHERE t.anidb_id=identified_anime.anidb_id "
            "AND (t.english_title {like} OR t.romaji_title {like}))",
        ),
        ids=('identified_anime.anidb_id',)),
    'recognized': Scope(
        fields=(
            "s.display_title {like}",
            "EXISTS (SELECT 1 FROM recognized_aliases x WHERE x.tvdb_series_id=s.tvdb_series_id "
            "AND x.tvdb_season=s.tvdb_season AND x.anidb_id=s.anidb_id AND x.alias_key {like})",
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
        clauses.append('(' + ' OR '.join(options) + ')')
    return ' AND '.join(clauses), params
