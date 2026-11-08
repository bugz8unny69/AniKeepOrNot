"""Short transactions for provider pacing/public data; caller holds admission.

No implicit DDL or migration. Network work must happen outside these methods.
TVDB credentials/tokens/response bodies are never accepted by public_put.
"""
from contextlib import contextmanager
import re
import sqlite3
import time
import uuid

from src.anime.history import integer
from src.anime.providers.errors import ProviderDeferred
from src.anime.storage.job_queue import QueueUnavailable
from src.anime.storage.migrations import LATEST_VERSION, MigrationError, _open
from src.anime.storage.sqlite import transaction

PROVIDERS = frozenset({"anidb", "anidb_titles", "anime_lists", "tvdb"})
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHE_ENTRIES = 130


class ProviderStore:
    def __init__(self, database, *, clock=None):
        self.database = database
        self.clock = clock or (lambda:time.time_ns()//1000000)

    @contextmanager
    def connection(self):
        db = None
        try:
            db = _open(self.database,readonly=False,timeout_ms=5000)
            db.execute("PRAGMA busy_timeout=5000")
            if db.execute("PRAGMA user_version").fetchone()[0] != LATEST_VERSION:
                raise QueueUnavailable()
            yield db
        except (OSError,sqlite3.Error,MigrationError):
            raise QueueUnavailable() from None
        finally:
            if db is not None: db.close()

    def now(self):
        return integer(self.clock())

    def _key(self, provider, key):
        if (provider not in {"anidb", "anidb_titles", "anime_lists"}
                or type(key) is not str or not re.fullmatch(r"[a-z0-9:_-]{1,64}",key)):
            raise ValueError("invalid public provider cache key")

    def public_get(self, provider, key):
        self._key(provider,key)
        with self.connection() as db:
            now = self.now()
            row = db.execute("SELECT payload FROM provider_public_cache WHERE provider=? AND cache_key=? "
                             "AND fetched_at_ms<=? AND expires_at_ms>?",(provider,key,now,now)).fetchone()
            return None if row is None else row[0]

    def reserve(self, provider, gap_ms, *, cache_key=None):
        """Atomically recheck cache then reserve; return (cached, token)."""
        if provider not in PROVIDERS or type(gap_ms) is not int or not 1000 <= gap_ms <= 86400000:
            raise ValueError("invalid provider reservation")
        if cache_key is not None: self._key(provider,cache_key)
        with self.connection() as db,transaction(db):
            now = self.now()
            if cache_key is not None:
                row = db.execute("SELECT payload FROM provider_public_cache WHERE provider=? AND cache_key=? "
                                 "AND fetched_at_ms<=? AND expires_at_ms>?",(provider,cache_key,now,now)).fetchone()
                if row is not None: return row[0],None
            row = db.execute("SELECT next_allowed_at_ms FROM provider_limits WHERE provider=?",(provider,)).fetchone()
            if row is not None and row[0]>now:
                raise ProviderDeferred(min(86400000,row[0]-now))
            token = uuid.uuid4().hex
            db.execute("INSERT INTO provider_limits(provider,next_allowed_at_ms,failures,reservation_token) VALUES(?,?,0,?) "
                       "ON CONFLICT(provider) DO UPDATE SET next_allowed_at_ms=excluded.next_allowed_at_ms,"
                       "reservation_token=excluded.reservation_token",(provider,now+gap_ms,token))
            return None,token

    def failure(self, provider, *, minimum_ms=10000, retry_after_ms=0):
        if provider not in PROVIDERS:
            raise ValueError("invalid provider")
        for value in (minimum_ms,retry_after_ms):
            if type(value) is not int or not 0 <= value <= 86400000:
                raise ValueError("invalid provider cooldown")
        with self.connection() as db,transaction(db):
            now = self.now()
            row = db.execute("SELECT failures FROM provider_limits WHERE provider=?",(provider,)).fetchone()
            if row is None: raise ValueError("request reservation required")
            wait = max(retry_after_ms,min(600000,max(1000,minimum_ms)*2**min(row[0],6)))
            db.execute("UPDATE provider_limits SET failures=MIN(16,failures+1),"
                       "next_allowed_at_ms=MAX(next_allowed_at_ms,?) WHERE provider=?",(now+wait,provider))

    def success(self, provider, token):
        with self.connection() as db,transaction(db):
            db.execute("UPDATE provider_limits SET failures=0 WHERE provider=? AND reservation_token=?",(provider,token))

    def public_put(self, provider, key, payload, ttl_ms, token):
        self._key(provider,key)
        if type(payload) is not bytes or not 1 <= len(payload) <= 67108864:
            raise ValueError("invalid public cache size")
        if type(ttl_ms) is not int or not 1000 <= ttl_ms <= 86400000:
            raise ValueError("invalid public cache expiry")
        with self.connection() as db,transaction(db):
            now = self.now()
            row = db.execute("SELECT reservation_token FROM provider_limits WHERE provider=?",(provider,)).fetchone()
            if row is None or row[0]!=token: return  # A newer request owns publication.
            db.execute("DELETE FROM provider_public_cache WHERE expires_at_ms<=? OR fetched_at_ms>?",(now,now))
            db.execute("INSERT INTO provider_public_cache VALUES(?,?,?,?,?) ON CONFLICT(provider,cache_key) "
                       "DO UPDATE SET fetched_at_ms=excluded.fetched_at_ms,expires_at_ms=excluded.expires_at_ms,payload=excluded.payload",
                       (provider,key,now,now+ttl_ms,payload))
            while True:
                count,size = db.execute("SELECT count(*),COALESCE(sum(length(payload)),0) FROM provider_public_cache").fetchone()
                if count<=MAX_CACHE_ENTRIES and size<=MAX_CACHE_BYTES: break
                db.execute("DELETE FROM provider_public_cache WHERE (provider,cache_key) IN "
                           "(SELECT provider,cache_key FROM provider_public_cache ORDER BY fetched_at_ms,provider,cache_key LIMIT 1)")
            db.execute("UPDATE provider_limits SET failures=0 WHERE provider=? AND reservation_token=?",(provider,token))
