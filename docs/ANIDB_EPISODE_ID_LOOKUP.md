# AniDB episode IDs: official HTTP API and local SQLite catalog

Anime-Lists supplies an AniDB anime ID and rules to translate AniDB episode
numbers into TVDB seasons and episodes. It does **not** contain AniDB's internal
episode record ID (eid). Before trial acquisition, AniKeepOrNot verifies
the Anime-Lists / TVDB result and retrieves eid from an AniDB anime episode
catalog, independently of Shoko.

## Configuration

Register an **HTTP API client** through AniDB's client registration process.
Provide the registered client identifier and its version in
`/etc/anime-discovery/settings.json`, within your existing `anidb` section:

```json
"anidb": {
  "enabled": true,
  "client": "your_registered_client_name",
  "clientver": 1
}
```

The `http_api_url` key from older builds is no longer necessary. **Remove any
HTTPS gateway URL** from this setting. If retained for compatibility, it must
be blank or equal to `http://api.anidb.net:9001/httpapi`.

The official read-only HTTP API endpoint is:

```text
http://api.anidb.net:9001/httpapi
```

For `request=anime`, the application submits `aid`, `client`,
`clientver`, and `protover=1`. No AniDB username or password is submitted.
**HTTP is unencrypted:** client identifiers, request metadata and response
contents can be read or altered in transit. Client registration is still
required, and rate-limit violations can affect the client or source IP. Do
not treat plaintext responses as cryptographically authenticated.

## Storage and request pacing

Metadata is stored in a separate indexed SQLite file alongside the daemon
state database:

```text
/var/lib/anime-discovery/anime-discovery.db   # jobs, approval, Keep, per-notification proof
/var/lib/anime-discovery/anime-metadata.db    # fetched AniDB anime and episode catalog
```

If the operational DB lives elsewhere, `anime-metadata.db` is created in the
same directory as the configured `daemon.database_path`.

The cache contains:
- `anidb_anime_catalog`: AniDB anime ID, last successful fetch, episode count
- `anidb_episode_catalog`: AniDB anime ID, regular episode number, exact eid
- `anidb_http_rate_limit`: shared next request time across worker processes

For a new anime, **one API request fetches the full episode catalog**. Known
positive episode IDs are served locally without another upstream request.
An absent episode number is rechecked no more than once per 24 hours per
anime. Upstream requests are spaced by at least 10 seconds across processes,
with an additional 10-minute cooldown following a failed/invalid API response.
Pending cooldowns defer worker identification without marking it as reviewed.

The `settings.json` file stays small; episode catalogs do not belong there.
The main operational DB retains only the verified internal episode ID linked to
an individual notification so Shoko can use that exact evidence after Keep.

## Existing data and fallback behavior

The previous `anidb-episode-cache/anime-<aid>.xml` files are not loaded by
the new SQLite catalog. They can be removed separately after validation; do
not delete or move the application state database.

A missing or invalid client name prevents new AniDB HTTP lookups. Already
reviewed `identity.episode_map` entries continue to work. If AniDB does not
yet include an episode or a mapping is ambiguous, no episode ID is invented.
The release remains in review after normal lookup retries.

Shoko is never queried for pre-acquisition episode identity. **Only when
you click Keep** does Sonarr import trial media into the permanent library.
The post-Keep Shoko monitor then reuses the persisted episode ID to help
recover an unrecognized file. `shoko.auto_link_verified` remains disabled
unless explicitly enabled by the user.

## Diagnosing an identity ParseError safely

If identity triage records `METADATA_UNAVAILABLE_IDENTITY_RESOLUTION`,
older releases do not say which parser or endpoint failed. New builds
log a structured warning for malformed XML, with a **fixed, known source
endpoint** (no client name or query parameters), HTTP status where available,
content type where available, response length, SHA-256 prefix, and a short
redacted printable preview. Example (illustrative, not an actual server result):

```text
upstream_xml_problem source=ANIDB_EPISODE_HTTP endpoint=http://api.anidb.net:9001/httpapi http_status=200 content_type=text/html bytes=38 sha256_12=... preview='<html>Unavailable</html>' reason=ValueError
Identity metadata retry notification=1 source=ANIDB_EPISODE_HTTP error_type=InvalidXMLResponse
```

The source may instead be `ANIDB_TITLES`, `ANIDB_TITLES_CACHE`,
`ANIME_LISTS`, or `ANIME_LISTS_MAPPING`. The endpoint shown is
a hardcoded public address, not the full request URL. HTTP status and
content type are `unknown` when the error is from an existing cache
or the upstream response does not expose them.

To inspect relevant log lines without making another AniDB request:

```bash
sudo journalctl -u anime-discovery --since '15 minutes ago' --no-pager |
  grep -E 'upstream_xml_problem|Identity metadata retry|identity notification='
```

This command only reads systemd logs. **Do not attach a raw HTTP request
with an AniDB client identifier or a TVDB API bearer token.** The
diagnostic preview is deliberately bounded and redacted.

An HTTP server error, invalid payload, bad XML, or invalid response root
from the AniDB episode API is a temporary metadata-source failure:
the worker retries with cooldown, remains `IDENTIFYING`, and does not
send an NTFY review notification. A valid AniDB XML document with no
matching episode remains a separate, unresolved-identity case.

## HTTP compression (October 2026 fix)

AniDB's HTTP API may return a gzip- or deflate-compressed payload while the
`Content-Type` is `text/xml` and the HTTP status is `200`. Python's
`urllib.request` does **not** transparently decompress `Content-Encoding`.
Prior builds tried to parse the compressed bytes as XML and logged
`ParseError` (often with unreadable binary preview).

The episode client now sends `Accept-Encoding: gzip, deflate`, decodes
supported encodings **before** parsing, and recognizes gzip/zlib signatures if
`Content-Encoding` is missing. It permits old uncompressed XML replies too.
Both downloaded and decompressed XML are size-limited. Only a validated
`<anime id="...">` episode catalog enters `anime-metadata.db`.

A short compressed response can be an AniDB `<error>` XML document. These
are not successful catalog lookups, even with HTTP 200. The diagnostic line
shows `content_encoding`, `encoded_bytes`, decoded `bytes`, a short
redacted preview of the **decoded** error, and `reason=ANIDB_ERROR_<code>`
(if AniDB supplies a code). Failed responses are not cached, and AniDB request
cooldowns still apply. NTFY is not sent for transient metadata failures.

After deployment, inspect only safe log lines:

```bash
sudo journalctl -u anime-discovery --since '15 minutes ago' --no-pager |
  grep -E 'upstream_xml_problem|AniDB episode catalog validated|Identity metadata retry|identity notification='
```

If a compressed upstream `<error>` response appears, use the **code** and
redacted message to diagnose client registration, rate limits, or API status.
Do not publish your full request URL or credential values. No additional
gateway or manual API request is needed for this change.
