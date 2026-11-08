"""Fixed provider endpoints, bounded responses, no redirect/secret diagnostics."""
from collections import OrderedDict
import copy
from dataclasses import dataclass, field
import datetime
import email.utils
import gzip
import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from src.anime.configuration_schema import ConfigurationError
from src.anime.providers.errors import (ProviderAuthenticationFailed, ProviderInvalidResponse,
                                          ProviderUnavailable)
from src.anime.providers import parsing
from src.anime.storage.provider_cache import ProviderStore

TITLES_URL = "https://anidb.net/api/anime-titles.xml.gz"
MAPPING_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list.xml"
ANIDB_URL = "http://api.anidb.net:9001/httpapi"
TVDB_URL = "https://api4.thetvdb.com/v4"


@dataclass(frozen=True)
class Options:
    client_name: str
    client_version: int
    timeout_seconds: int = 30

    def __post_init__(self):
        if type(self.client_name) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{3,32}",self.client_name):
            raise ConfigurationError()
        if type(self.client_version) is not int or not 1 <= self.client_version <= 999999:
            raise ConfigurationError()
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ConfigurationError()


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)

    def __post_init__(self):
        if (type(self.api_key) is not str or not self.api_key.strip() or len(self.api_key)>4096
                or any(ord(char)<32 or ord(char)==127 for char in self.api_key)):
            raise ProviderAuthenticationFailed()

    def fingerprint(self):
        return hashlib.sha256(json.dumps([self.api_key]).encode()).hexdigest()


class MemoryCache:
    """Process-local bounded LRU; secrets and TVDB responses stay off disk."""
    def __init__(self, *, clock=time.monotonic):
        self.clock, self.entries, self.lock = clock, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            now = self.clock()
            if entry is None: return None
            created,expiry,value = entry
            if not created<=now<expiry:
                self.entries.pop(key,None)
                return None
            self.entries.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key, value, ttl):
        with self.lock:
            now = self.clock()
            self.entries[key] = (now,now+ttl,copy.deepcopy(value))
            self.entries.move_to_end(key)
            while len(self.entries)>128: self.entries.popitem(last=False)

    def forget(self, key):
        with self.lock: self.entries.pop(key,None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_after(value, now):
    try:
        seconds = int(value)
    except (ValueError,TypeError):
        try:
            stamp = email.utils.parsedate_to_datetime(value)
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=datetime.timezone.utc)
            seconds = int(stamp.timestamp()-now/1000)+1
        except (ValueError,TypeError,OverflowError): return 0
    return max(0,min(86400000,seconds*1000))


def decode(payload, encoding, limit):
    if type(payload) is not bytes or not payload: raise ProviderInvalidResponse()
    codec = (encoding or "").strip().lower()
    if codec in ("","identity"):
        if payload.startswith(b"\x1f\x8b"): codec="gzip"
        elif len(payload)>=2 and payload[0]&15==8 and (payload[0]*256+payload[1])%31==0: codec="deflate"
        else:
            if len(payload)>limit: raise ProviderInvalidResponse()
            return payload
    try:
        if codec in ("gzip","x-gzip"):
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                plain = stream.read(limit+1)
        elif codec=="deflate":
            obj = zlib.decompressobj()
            plain = obj.decompress(payload,limit+1)
            if obj.unconsumed_tail or obj.unused_data or not obj.eof: raise ValueError()
        else: raise ValueError()
        if not plain or len(plain)>limit: raise ValueError()
        return plain
    except (ValueError,OSError,EOFError,zlib.error):
        raise ProviderInvalidResponse() from None


def json_object(payload):
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    def constant(value): raise ValueError()
    try:
        value = json.loads(payload.decode("utf-8"),object_pairs_hook=pairs,parse_constant=constant)
        if type(value) is not dict: raise ValueError()
        return value
    except (ValueError,UnicodeError,RecursionError):
        raise ProviderInvalidResponse() from None


class Clients:
    """Blocking methods: async resolver offloads these, retaining job admission."""
    def __init__(self, database, options, *, opener=None, store=None, memory=None):
        if type(options) is not Options: raise ValueError("provider options required")
        self.options = options
        self.store = store or ProviderStore(database)
        self.memory = memory or MemoryCache()
        self.open = opener or urllib.request.build_opener(NoRedirect()).open

    def _fetch(self, provider, token, url, *, body=None, bearer=None, limit=4194304):
        headers = {"User-Agent":"AnimeDiscoveryNextGen/1.0","Accept-Encoding":"gzip, deflate"}
        if body is not None: headers["Content-Type"]="application/json"
        if bearer is not None: headers["Authorization"]="Bearer "+bearer
        request = urllib.request.Request(url,data=body,headers=headers)
        try:
            with self.open(request,timeout=self.options.timeout_seconds) as response:
                status = response.status
                if status!=200: raise urllib.error.HTTPError(url,status,"",response.headers,None)
                raw = response.read(min(limit,33554432)+1)
                if len(raw)>min(limit,33554432): raise ProviderInvalidResponse()
                return decode(raw,response.headers.get("Content-Encoding"),limit)
        except urllib.error.HTTPError as exc:
            exc.close()
            self.store.failure(provider,minimum_ms=600000 if provider=="anidb" else 10000,
                               retry_after_ms=retry_after(exc.headers.get("Retry-After") if exc.headers else None,self.store.now()))
            if exc.code in (401,403) and provider=="tvdb":
                if bearer is None or exc.code==403: raise ProviderAuthenticationFailed() from None
                raise ProviderUnavailable() from None
            raise ProviderUnavailable() from None
        except (OSError,TimeoutError):
            self.store.failure(provider,minimum_ms=600000 if provider=="anidb" else 10000)
            raise ProviderUnavailable() from None
        except ProviderInvalidResponse:
            self.store.failure(provider,minimum_ms=600000 if provider=="anidb" else 10000)
            raise

    def _public(self, provider, key, url, *, limit, gap, validate):
        cached,token = self.store.reserve(provider,gap,cache_key=key)
        if cached is not None:
            # Cached bytes remain input, not an exemption from verification.
            validate(cached)
            return cached
        payload = self._fetch(provider,token,url,limit=limit)
        try:
            validate(payload)
        except ProviderInvalidResponse:
            self.store.failure(provider,minimum_ms=600000 if provider=="anidb" else 10000)
            raise
        self.store.public_put(provider,key,payload,86400000,token)
        return payload

    def title_candidates(self, aliases, *, threshold_pct=50):
        payload = self._public("anidb_titles","titles",TITLES_URL,limit=67108864,gap=86400000,
                               validate=lambda value:parsing.xml(value,"animetitles"))
        return parsing.title_matches(payload,aliases,threshold_pct=threshold_pct)

    def _mapping_payload(self):
        return self._public("anime_lists","mapping",MAPPING_URL,limit=16777216,gap=60000,
                            validate=lambda value:parsing.xml(value,"anime-list",limit=16777216))

    def mapping(self, aid):
        return parsing.mapping_id(self._mapping_payload(),aid)

    def season_entry(self, tid, season, episode=None):
        return parsing.season_entry(self._mapping_payload(),tid,season,episode)

    def _anime_payload(self, aid):
        parsing.positive_text(str(aid))
        query = urllib.parse.urlencode({"request":"anime","aid":aid,"protover":1,
                                      "client":self.options.client_name,"clientver":self.options.client_version})
        payload = self._public("anidb","anime:"+str(aid),ANIDB_URL+"?"+query,limit=4194304,gap=10000,
                               validate=lambda value:parsing.anime_record(value,aid))
        return payload

    def anime(self, aid):
        return parsing.anime_record(self._anime_payload(aid),aid)

    def episodes(self, aid):
        # Same cached full-anime response, no second request per episode. A
        # missing regular number remains missing until the 24-hour refresh.
        return parsing.episode_catalog(self._anime_payload(aid),aid)

    def airdates(self, aid):
        return parsing.episode_airdates(self._anime_payload(aid),aid)

    def _tvdb(self, tid, credentials, key_parts, path, parse, ttl):
        parsing.positive_text(str(tid))
        if type(credentials) is not Credentials: raise ProviderAuthenticationFailed()
        fingerprint = credentials.fingerprint()
        key = (*key_parts,tid,fingerprint)
        cached = self.memory.get(key)
        if cached is not None: return cached
        token_key = ("token",fingerprint)
        bearer = self.memory.get(token_key) or self.tvdb_login(credentials)
        # The shared login reservation can defer this GET. Next attempt reuses
        # the in-memory token instead of repeatedly authenticating.
        _,reservation = self.store.reserve("tvdb",1000)
        try:
            raw = self._fetch("tvdb",reservation,TVDB_URL+path,bearer=bearer)
        except (ProviderUnavailable,ProviderAuthenticationFailed):
            self.memory.forget(token_key)
            raise
        try:
            value = parse(json_object(raw))
        except ProviderInvalidResponse:
            self.store.failure("tvdb")
            raise
        self.memory.put(key,value,ttl)
        self.store.success("tvdb",reservation)
        return value


    def tvdb_login(self, credentials):
        """Log in to TVDB and return the bearer token, kept in memory for 30 minutes.

        Raises ProviderAuthenticationFailed when TVDB rejects the API key.
        """
        if type(credentials) is not Credentials: raise ProviderAuthenticationFailed()
        _,reservation = self.store.reserve("tvdb",1000)
        body = {"apikey":credentials.api_key}
        raw = self._fetch("tvdb",reservation,TVDB_URL+"/login",body=json.dumps(body).encode())
        try:
            document = json_object(raw)
            bearer = document.get("data",{}).get("token") if type(document.get("data")) is dict else None
            if type(bearer) is not str or not bearer or len(bearer)>8192 or any(ord(c)<=32 or ord(c)==127 for c in bearer):
                raise ProviderInvalidResponse()
        except ProviderInvalidResponse:
            self.store.failure("tvdb")
            raise
        self.memory.put(("token",credentials.fingerprint()),bearer,1800)
        self.store.success("tvdb",reservation)
        return bearer

    def anidb_check(self, aid=1):
        """One paced AniDB request that proves the client name is accepted.

        Returns None on success, or AniDB's own error text (for example
        "client version missing or invalid" or "Banned"). AniDB reports these
        with HTTP 200 and an <error> document. The answer is cached like any
        other anime record, so it is never requested twice in 24 hours.
        Raises ProviderDeferred during a cooldown and ProviderUnavailable when
        AniDB cannot be reached; both are recorded by the shared pacing.
        """
        key = "anime:"+str(aid)
        cached,token = self.store.reserve("anidb",10000,cache_key=key)
        if cached is not None: return None
        query = urllib.parse.urlencode({"request":"anime","aid":aid,"protover":1,
                                      "client":self.options.client_name,"clientver":self.options.client_version})
        payload = self._fetch("anidb",token,ANIDB_URL+"?"+query)
        try:
            error = parsing.xml(payload,"error")
        except ProviderInvalidResponse:
            error = None
        if error is not None:
            self.store.failure("anidb",minimum_ms=600000)
            text = " ".join((error.text or "").split())[:200]
            return text or "unexplained error"
        try:
            parsing.anime_record(payload,aid)
        except ProviderInvalidResponse:
            self.store.failure("anidb",minimum_ms=600000)
            raise
        self.store.public_put("anidb",key,payload,86400000,token)
        return None

    def series(self, tid, credentials):
        return self._tvdb(tid,credentials,("series",),"/series/"+str(tid)+"/extended?meta=translations",
                          lambda data:parsing.tvdb_record(data,tid),900)

    def episode_page(self, tid, page, credentials):
        if type(page) is not int or not 0<=page<20: raise ValueError("invalid episode page")
        return self._tvdb(tid,credentials,("episodes_official",page),"/series/"+str(tid)+"/episodes/official?page="+str(page),
                          lambda data:parsing.tvdb_episode_page(data,tid),300)

    def mapping_xml(self):
        return self._public("anime_lists","mapping",MAPPING_URL,limit=16777216,gap=60000,
                            validate=lambda value:parsing.xml(value,"anime-list",limit=16777216))
