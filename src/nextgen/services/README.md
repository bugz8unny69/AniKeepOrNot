# Service health

Anime Discovery depends on several external services. This package gives each service one **adapter**, keeps them in a **registry**, and tracks their health with one **monitor**. Nothing outside this package keeps its own list of services.

| Service | Adapter | How health is checked |
|---|---|---|
| qBittorrent | `adapters/qbittorrent.py` | Log in, then confirm a supported version (4.5+, Web API 2.8.14+) |
| Prowlarr | `adapters/arr.py` | `GET /api/v1/system/status` with the API key |
| Sonarr | `adapters/arr.py` | `GET /api/v3/system/status` with the API key; v4 required |
| Jellyfin *(optional)* | `adapters/media_servers.py` | `GET /System/Info` with the API key |
| Shoko *(optional)* | `adapters/media_servers.py` | `GET /api/v3/Init/Version` |
| AniDB | `adapters/metadata.py` | **Local records only, never contacted** (see below) |
| Anime-Lists | `adapters/metadata.py` | Local records only |
| TVDB | `adapters/metadata.py` | Local records only |

## Statuses

| Status | Meaning | Blocks work? |
|---|---|---|
| `UP` | Reachable, and our credentials were accepted | no |
| `DOWN` | Could not be reached, or did not answer properly | **yes** |
| `NOT_CONFIGURED` | The address or credentials are missing or not valid | **yes** |
| `AUTH_FAILED` | Reachable, but our credentials were rejected | **yes** |
| `UNSUPPORTED` | Reachable, but the version is not supported | **yes** |
| `DISABLED` | An optional service that is turned off (blank address) | no |
| `UNKNOWN` | Not checked yet, or its record could not be read | no; a real request decides |

Every message names the service and says what is wrong, for example: `Prowlarr is unavailable: HTTP 503 at http://prowlarr:9696`.

## Checking services

```sh
sudo adm services check                    # every service, as a table
sudo adm services check --service prowlarr # one service
sudo adm services check --json             # for scripts
```

Exit status: `0` if every service is usable, `1` if any is down, not set up, rejected the credentials or is unsupported, and `2` if the settings cannot be read.

## AniDB, Anime-Lists and TVDB

AniDB bans clients that send too many requests, so these providers are never contacted just to check health (`network_probe = False`). Their adapters read two tables that every real request already maintains (`providers/http.py`):

- `provider_limits`: consecutive failed requests, and the cooldown before the next one is allowed;
- `provider_public_cache`: the downloaded data (for example the AniDB titles dump) and when it was fetched.

A provider is `DOWN` while it has recent failures, and `UP` with the age of its newest cached data otherwise.

## Passive marking

When a real request fails, the code that made it calls `ServiceHealth.mark_failed(service, exc)`. The adapter's `classify(exc)` turns the client's own exception into a status, so the service is marked down at once, without an extra network call.

Expected waits, such as a planned AniDB pacing delay, return `NOT_A_FAILURE` and never mark a service down. Each status change is returned once as a `Transition`, so it is logged once instead of on every attempt.

## Adding a service

1. Create `adapters/<name>.py` with a subclass of `ServiceAdapter`:

   ```python
   from src.nextgen.services.base import DOWN, NOT_CONFIGURED, UP, ServiceAdapter

   class ExampleAdapter(ServiceAdapter):
       name = 'example'            # lowercase; used in settings, logs and job requirements
       label = 'Example'           # shown to people
       optional = False            # True: a blank address means DISABLED rather than NOT_CONFIGURED
       network_probe = True        # False if probe() must never contact the service

       def probe(self, configuration, secrets, *, now_ms=0):
           ...                     # return self.result(UP, 'version 1.2', now_ms=now_ms)

       def classify(self, exc):
           ...                     # map the client's exceptions to a status, or None
   ```

   For an HTTP service with a status endpoint, reuse `adapters/_http.py` (`base_url`, `get_status`), as Prowlarr and Jellyfin do.
2. Add the class to `BUILTIN` in `adapters/__init__.py`. External plugins can call `ServiceRegistry.register(adapter)` instead.
3. Add tests that use a fake transport, never a real service (see `tests/test_service_adapters.py`).

## Roadmap

1. **This phase:** adapters, registry, monitor and `adm services check`. Job processing does not change yet.
2. **Next:** jobs declare which services they need, and the worker skips jobs whose services are down. This replaces `QbittorrentReadiness` and `ACQUISITION_KINDS`.
3. **Then:** an outage means waiting, not failing. The generic `SERVICE_UNAVAILABLE` code names the service, and a job gives up after **72 hours**. The old per-service `*_UNAVAILABLE` codes are migrated.
4. **Then:** startup fails when a required service is not set up correctly; a dashboard banner shows each service's status.
