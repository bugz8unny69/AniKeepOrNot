"""AKON SQLite: the schema of a new database (the series job and what it keeps), connections."""
from contextlib import contextmanager
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_identity_matches (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season BETWEEN -1 AND 999),
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 512),
 PRIMARY KEY(alias_key,tvdb_season)
);
CREATE TABLE IF NOT EXISTS anime_title_metadata (
 anidb_id INTEGER PRIMARY KEY,
 english_title TEXT CHECK(english_title IS NULL OR length(trim(english_title)) BETWEEN 1 AND 512),
 romaji_title TEXT CHECK(romaji_title IS NULL OR length(trim(romaji_title)) BETWEEN 1 AND 512),
 CHECK(english_title IS NOT NULL OR romaji_title IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS provider_limits (
 provider TEXT PRIMARY KEY CHECK(provider IN ('anidb','anidb_titles','anime_lists','tvdb')),
 next_allowed_at_ms INTEGER NOT NULL CHECK(next_allowed_at_ms >= 0),
 failures INTEGER NOT NULL DEFAULT 0 CHECK(failures BETWEEN 0 AND 16),
 reservation_token TEXT NOT NULL CHECK(length(reservation_token)=32)
);
CREATE TABLE IF NOT EXISTS provider_public_cache (
 provider TEXT NOT NULL CHECK(provider IN ('anidb','anidb_titles','anime_lists')),
 cache_key TEXT NOT NULL CHECK(length(cache_key) BETWEEN 1 AND 64),
 fetched_at_ms INTEGER NOT NULL CHECK(fetched_at_ms >= 0),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > fetched_at_ms),
 payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload) BETWEEN 1 AND 67108864),
 PRIMARY KEY(provider,cache_key)
);
CREATE INDEX IF NOT EXISTS idx_provider_cache_expiry ON provider_public_cache(expires_at_ms);
CREATE TABLE IF NOT EXISTS recognized_seasons (
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 anidb_id INTEGER NOT NULL,
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE TABLE IF NOT EXISTS recognized_series (
 anidb_id INTEGER PRIMARY KEY,
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 anime_type TEXT CHECK(anime_type IS NULL OR length(trim(anime_type)) BETWEEN 1 AND 64),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0)
);
CREATE TABLE IF NOT EXISTS recognized_aliases (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 anidb_id INTEGER NOT NULL REFERENCES recognized_series(anidb_id),
 PRIMARY KEY(alias_key,anidb_id)
);
CREATE INDEX IF NOT EXISTS idx_recognized_alias_series ON recognized_aliases(anidb_id);
CREATE TABLE IF NOT EXISTS recognized_episodes (
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES recognized_series(anidb_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(anidb_id,anidb_episode_number),
 UNIQUE(anidb_episode_id),
 UNIQUE(tvdb_series_id,tvdb_season,tvdb_episode),
 UNIQUE(tvdb_episode_id),
 CHECK((tvdb_series_id IS NULL AND tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_series_id IS NOT NULL AND tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_recognized_episodes_season ON recognized_episodes(tvdb_series_id,tvdb_season);
CREATE TABLE IF NOT EXISTS service_status (
 service TEXT PRIMARY KEY CHECK(length(service) BETWEEN 1 AND 64),
 label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 64),
 status TEXT NOT NULL CHECK(status IN ('UP','DOWN','NOT_CONFIGURED','AUTH_FAILED','UNSUPPORTED','DISABLED','UNKNOWN')),
 detail TEXT NOT NULL DEFAULT '' CHECK(length(detail) <= 500),
 since_ms INTEGER NOT NULL CHECK(since_ms >= 0),
 checked_at_ms INTEGER NOT NULL CHECK(checked_at_ms >= 0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0)
);
CREATE TABLE IF NOT EXISTS identity_requests (
 request_id INTEGER PRIMARY KEY,
 infohash TEXT UNIQUE CHECK(infohash IS NULL OR (length(infohash)=40 AND infohash NOT GLOB '*[^0-9a-f]*')),
 raw_body BLOB NOT NULL CHECK(length(raw_body) BETWEEN 1 AND 65536),
 process_anyway INTEGER NOT NULL DEFAULT 0 CHECK(process_anyway IN (0,1)),
 received_at_ms INTEGER NOT NULL CHECK(received_at_ms>=0),
 next_try_at_ms INTEGER NOT NULL CHECK(next_try_at_ms>=0),
 tries INTEGER NOT NULL DEFAULT 0 CHECK(tries>=0),
 last_reason_code TEXT,
 outcome TEXT CHECK(outcome IS NULL OR outcome IN ('accepted','rejected')),
 decided_at_ms INTEGER CHECK(decided_at_ms IS NULL OR decided_at_ms>=0),
 CHECK((outcome IS NULL)=(decided_at_ms IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_identity_requests_next ON identity_requests(next_try_at_ms,request_id);
CREATE TABLE IF NOT EXISTS series (
 anidb_id INTEGER PRIMARY KEY,
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('WORKING','ASLEEP','KEPT','REJECTED')),
 wake_at_ms INTEGER,
 torrents_at_once INTEGER NOT NULL DEFAULT 1,
 torrents_json TEXT NOT NULL,
 episodes_json TEXT NOT NULL,
 recent_json TEXT NOT NULL,
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,
 work_json TEXT NOT NULL DEFAULT '{}',
 promoted_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_series_state_wake ON series(state, wake_at_ms);
CREATE TABLE IF NOT EXISTS notifications (
 notification_id INTEGER PRIMARY KEY,
 type TEXT NOT NULL CHECK(type IN ('API_REJECTION','SERIES_EPISODE_TROUBLE','KEEP_SEARCH_FAILED','TRANSCODE_BATCH_DONE','MOVIE_SKIPPED','RELEASE_UNSUPPORTED','JOB_FAILED')),
 severity TEXT NOT NULL CHECK(severity IN ('info','attention')),
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
 message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 500),
 anidb_id INTEGER CHECK(anidb_id IS NULL OR anidb_id>0),
 job_id INTEGER CHECK(job_id IS NULL OR job_id>0),
 release_name TEXT CHECK(release_name IS NULL OR length(release_name) BETWEEN 1 AND 1024),
 group_key TEXT CHECK(group_key IS NULL OR length(group_key) BETWEEN 1 AND 200),
 raw_body BLOB CHECK(raw_body IS NULL OR length(raw_body)<=65536),
 count INTEGER NOT NULL CHECK(count>=1),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 last_at_ms INTEGER NOT NULL CHECK(last_at_ms>=created_at_ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_group ON notifications(group_key) WHERE group_key IS NOT NULL;
"""

def connect(path):
    from src.anime.storage.adapter import BUSY_TIMEOUT_MS, configure
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS/1000)
    configure(db)
    db.execute("PRAGMA journal_mode=WAL")
    return db

@contextmanager
def admitted_connection(gate):
    """New runtime operations use admission, then open/close their connection.

    For an entire job attempt, hold gate.operation() around handler execution
    instead; that admission also covers short-lived connections and heartbeats.
    Low-level connect() is retained for explicit bootstrap and offline fixtures.
    Never nest admissions or use this during exclusive migration maintenance.
    """
    with gate.operation():
        from src.anime.storage.migrations import _open
        from src.anime.storage.adapter import require_current
        db = _open(gate.database, readonly=False,timeout_ms=5000)
        try:
            require_current(db)
            db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()


def initialize(db):
    """Initialize a *new* NextGen database at schema LATEST_VERSION; never implicitly upgrade.

    Existing v1/v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17/v18/v19/v20/v21/v22 databases require explicit versioned SQL migrations
    before accessing v50 storage.
    """
    from src.anime.storage.migrations import LATEST_VERSION
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == LATEST_VERSION:
        return
    if version != 0:
        raise ValueError("existing NextGen database requires versioned SQL upgrade")
    present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    if present:
        raise ValueError("database has tables but no schema version; refuse implicit upgrade")
    if db.in_transaction:
        raise ValueError("initialization requires no active transaction")
    try:
        db.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version={LATEST_VERSION};\nCOMMIT;")
    except BaseException:
        db.rollback()
        raise


@contextmanager
def transaction(db):
    """Own one atomic unit; reject nesting rather than committing caller work.

    Writers queue for the write lock through the SQLite adapter instead of
    failing with "database is locked" (see storage/adapter.py).
    """
    from src.anime.storage.adapter import write_transaction
    with write_transaction(db):
        yield





class IdentityConflict(ValueError):
    reason_code = "IDENTITY_CONFLICT"

    def __init__(self):
        super().__init__("Conflicting anime information needs review.")


class IdentityNotReady(ValueError):
    reason_code = "IDENTITY_INCOMPLETE"

    def __init__(self):
        super().__init__("Anime identification is incomplete.")


class AniDBEntryMissing(IdentityNotReady):
    """No AniDB entry matches the release title (or it has none): unsupported."""
    reason_code = "ANIDB_ENTRY_MISSING"


class AnimeListsEntryMissing(IdentityNotReady):
    """AniDB has the anime, but Anime-Lists links it to no TVDB series."""
    reason_code = "ANIME_LISTS_ENTRY_MISSING"


class AnimeListsMovie(AnimeListsEntryMissing):
    """Anime-Lists records the AniDB entry as a movie (tvdbid="movie"): movies are skipped."""
    reason_code = "RELEASE_MOVIE_SKIPPED"



def upsert_anime_titles(db, anidb_id, english_title, romaji_title):
    """Preserve known translations; never overwrite a verified identity or alias."""
    if not english_title and not romaji_title:
        return
    for title in (english_title, romaji_title):
        if title is not None and (type(title) is not str or not 1 <= len(title.strip()) <= 512):
            raise ValueError("Invalid translated anime title")
    db.execute(
        "INSERT INTO anime_title_metadata(anidb_id,english_title,romaji_title) VALUES(?,?,?) "
        "ON CONFLICT(anidb_id) DO UPDATE SET "
        "english_title=COALESCE(anime_title_metadata.english_title,excluded.english_title), "
        "romaji_title=COALESCE(anime_title_metadata.romaji_title,excluded.romaji_title)",
        (anidb_id, english_title, romaji_title))
