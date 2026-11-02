"""Display-only anime title selection shared by the API and Jellyfin NFOs."""


def preferred_title(canonical, english, romaji, language):
    if language == 'english':
        return english or canonical
    if language == 'japanese':
        return romaji or canonical
    raise ValueError('Invalid anime title language')


def title_for(db, aid, canonical, language):
    """Trust a verified AniDB ID; never infer a title from a TVDB series ID."""
    row = db.execute(
        'SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?',
        (aid,)).fetchone()
    return preferred_title(canonical, row[0] if row else None,
                           row[1] if row else None, language)
