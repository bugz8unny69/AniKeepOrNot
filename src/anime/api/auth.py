"""Separate webhook/management credentials and bounded process-local sessions."""
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit
import ipaddress
import re

COOKIE='ad_nextgen_session'
TOKEN=re.compile(r'[A-Za-z0-9_-]{32,256}')


class ApiError(ValueError):
    def __init__(self,status,reason,*,detail=None):
        # detail: a specific, already safe message that replaces the generic
        # catalog message, e.g. "Sonarr is unavailable: HTTP 503 at ...".
        self.status,self.reason_code,self.detail=status,reason,detail
        super().__init__('The API request could not be completed.')


def matches(supplied,expected):
    return (type(supplied) is str and bool(expected) and TOKEN.fullmatch(supplied) is not None
            and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),hashlib.sha256(expected.encode()).digest()))


def bearer(headers):
    value=headers.get('authorization')
    if value is None:return None
    parts=value.split(' ')
    if len(parts)!=2 or parts[0].lower()!='bearer' or not TOKEN.fullmatch(parts[1]):raise ApiError(401,'API_AUTH_REQUIRED')
    return parts[1]


def public_origin(value):
    try:
        parsed=urlsplit(value)
        if (len(value)>2048 or not value.isascii() or any(c in value for c in ('\\','?','#')) or parsed.scheme not in ('https','http') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.path not in ('','/')
            or parsed.query or parsed.fragment or any(ord(c)<=32 or ord(c)==127 for c in value)):raise ValueError()
        port=parsed.port
        if parsed.scheme=='http' and parsed.hostname!='localhost' and not ipaddress.ip_address(parsed.hostname).is_loopback:raise ValueError()
        host=parsed.hostname.lower()
        authority=('['+host+']' if ':' in host else host)
        if port is not None and port!=(443 if parsed.scheme=='https' else 80):authority+=':'+str(port)
        return parsed.scheme+'://'+authority
    except (ValueError,TypeError,AttributeError):raise ApiError(400,'API_ORIGIN_INVALID') from None


def valid_host(host):
    """Validate the immediate request authority, without trusting forwarded headers."""
    if not isinstance(host,str) or not host or len(host)>255 or any(c in host for c in ('/', '\\', '@', ' ', '\t', '#', '?', ',')):
        return False
    try:
        parsed=urlsplit('http://'+host)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        port=parsed.port
        return parsed.netloc.lower()==host.lower() and (port is None or 1<=port<=65535)
    except ValueError:
        return False


def check_origin(headers,origin,*,required=False):
    """Match browser Origin to request Host, not the application's bind address.

    Nginx must preserve Host. Ignore X-Forwarded-Host/Proto for authority,
    since they can be supplied by clients unless the proxy sanitizes them.
    """
    host=headers.get('host','')
    if not valid_host(host):
        raise ApiError(403,'API_ORIGIN_INVALID')
    found=headers.get('origin')
    if required and found is None:
        raise ApiError(403,'API_ORIGIN_INVALID')
    if found is not None:
        try:
            parsed=urlsplit(found)
            normalized=public_origin(found)
            # A browser may spell out a scheme's default port. Normalize
            # both authorities before comparison while retaining the strict
            # actual Host check (never trust X-Forwarded-Host).
            host_parts=urlsplit(parsed.scheme+'://'+host)
            if (host_parts.hostname is None or
                    host_parts.hostname.lower()!=parsed.hostname.lower() or
                    (host_parts.port or (443 if parsed.scheme=='https' else 80)) !=
                    (parsed.port or (443 if parsed.scheme=='https' else 80))):
                raise ValueError()
        except (ValueError,ApiError):
            raise ApiError(403,'API_ORIGIN_INVALID') from None
    if headers.get('sec-fetch-site') not in (None,'same-origin','none'):
        raise ApiError(403,'API_ORIGIN_INVALID')


def webhook_key(headers,query,body,expected):
    if not expected:raise ApiError(503,'API_AUTH_UNCONFIGURED')
    supplied=[]
    if 'authorization' in headers:supplied.append(bearer(headers))
    if 'token' in query:
        if len(query['token'])!=1:raise ApiError(401,'API_AUTH_REQUIRED')
        supplied.append(query['token'][0])
    if 'token' in body:supplied.append(body['token'])
    if not supplied or not all(matches(value,expected) for value in supplied):raise ApiError(401,'API_AUTH_REQUIRED')


@dataclass(frozen=True)
class Session:
    expires: float
    key_digest: bytes = field(repr=False)
    csrf: str = field(repr=False)


class Sessions:
    def __init__(self,*,clock=time.monotonic,ttl=43200,capacity=1024):
        self.clock,self.ttl,self.capacity=clock,ttl,capacity
        self.items=OrderedDict();self.failures=OrderedDict();self.lock=threading.Lock()
    def _purge(self):
        now=self.clock()
        for key in list(self.items):
            if self.items[key].expires<=now:del self.items[key]
        for key in list(self.failures):
            if self.failures[key][0]+60<=now:del self.failures[key]
    def attempt(self,peer):
        # Count every attempt, including concurrent and successful attempts.
        with self.lock:
            self._purge();start,count=self.failures.get(peer,(self.clock(),0))
            if count>=10:raise ApiError(429,'API_RATE_LIMITED')
            if peer not in self.failures and len(self.failures)>=2048:raise ApiError(429,'API_RATE_LIMITED')
            self.failures[peer]=(start,count+1)
    def create(self,key):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with self.lock:
            self._purge()
            if len(self.items)>=self.capacity:raise ApiError(429,'API_RATE_LIMITED')
            self.items[hashlib.sha256(token.encode()).digest()]=Session(self.clock()+self.ttl,hashlib.sha256(key.encode()).digest(),csrf)
        return token,csrf
    def require(self,headers,key,*,mutation=False):
        raw=headers.get('cookie','')
        if len(raw)>4096:raise ApiError(401,'API_AUTH_REQUIRED')
        values=[p.strip().split('=',1)[1] for p in raw.split(';') if p.strip().startswith(COOKIE+'=')]
        if len(values)!=1 or not TOKEN.fullmatch(values[0]):raise ApiError(401,'API_AUTH_REQUIRED')
        digest=hashlib.sha256(values[0].encode()).digest()
        with self.lock:
            self._purge();session=self.items.get(digest)
            if session is None or not hmac.compare_digest(session.key_digest,hashlib.sha256(key.encode()).digest()):raise ApiError(401,'API_AUTH_REQUIRED')
            if mutation and not matches(headers.get('x-csrf-token'),session.csrf):raise ApiError(403,'API_CSRF_REQUIRED')
        return digest,session
    def remove(self,digest):
        with self.lock:self.items.pop(digest,None)

    def clear(self):
        with self.lock:
            self.items.clear()
