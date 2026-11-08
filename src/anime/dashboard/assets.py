"""Exact public asset allowlist. Never serve a caller-supplied filesystem path."""
from pathlib import Path
from html import escape

ROOT=Path(__file__).with_name('static')
FILES={
    '/':('index.html','text/html; charset=utf-8'),
    '/recognized':('index.html','text/html; charset=utf-8'),
    '/jobs':('index.html','text/html; charset=utf-8'),
    '/settings':('index.html','text/html; charset=utf-8'),
    '/assets/dashboard.css':('dashboard.css','text/css; charset=utf-8'),
    '/assets/dashboard.js':('dashboard.js','text/javascript; charset=utf-8'),
}
CSP=("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
     "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")


def load_assets():
    """Trusted, bounded source assets loaded once at process construction."""
    result={}
    for route,(name,mime) in FILES.items():
        content=(ROOT/name).read_bytes()
        if len(content)>262144:raise ValueError('Dashboard asset exceeds its bound.')
        result[route]=(mime,content)
    return result


def address_mismatch(origin):
    """Static, unauthenticated HTML help for a misdirected dashboard navigation.

    The configured canonical origin is validated during Application construction.
    Do not reflect the caller's Host header or proxy-supplied URLs.
    """
    safe=escape(origin,quote=True)
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard address mismatch · Anime Discovery</title></head><body>
<main><h1>Anime Discovery dashboard address mismatch</h1>
<p>The address opened in your browser does not match the address configured for this dashboard.</p>
<p>Configured address: <a href="'''+safe+'''">'''+safe+'''</a></p>
<p>If you normally open Anime Discovery through a reverse proxy, configure its public origin
to that browser-facing HTTPS address, including any nonstandard port, then restart the API.
Do not change the browser address to an internal loopback address from another computer.</p>
</main></body></html>''').encode('utf-8')
