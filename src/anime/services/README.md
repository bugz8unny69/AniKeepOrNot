# Service health

Anime Discovery depends on several external services. This package gives each service one **adapter**, keeps them in a **registry**, and tracks their health with one **monitor**. Nothing outside this package keeps its own list of services.

| Service | Adapter | How health is checked | At worker startup |
|---|---|---|---|
| qBittorrent **(required)** | `adapters/qbittorrent.py` | Log in, then confirm a supported version (4.5+, Web API 2.8.14+) | Same check |
| Prowlarr *(optional)* | `adapters/arr.py` | `GET /api/v1/system/status` with the API key | Same check |
| Sonarr *(optional)* | `adapters/arr.py` | `GET /api/v3/system/status` with the API key; v4 required | Same check |
| Jellyfin *(optional)* | `adapters/media_servers.py` | `GET /System/Info` with the API key | Same check |
| Shoko *(optional)* | `adapters/media_servers.py` | `GET /api/v3/Init/Version` | Same check |
| AniDB **(required)** | `adapters/metadata.py` | **Local records only, never contacted** (see below) | One paced request, with safeguards |
| Anime-Lists | `adapters/metadata.py` | Local records only | Local records only |
| TVDB **(required)** | `adapters/metadata.py` | Local records only | One login |

## Required and optional services

- **Required** (`required = True`): qBittorrent, AniDB and TVDB. If one of them is not set up, rejects the login, client name or API key, or is an unsupported version, **the worker does not start**. It logs one `SERVICE_STARTUP_REFUSED` line per service (reason `SERVICE_SETUP_INVALID`), prints the same lines, and exits with code **78**. The worker unit has `RestartPreventExitStatus=78`, so systemd does not restart it every 10 seconds (which would also send AniDB a request each time). Fix the setting, then start the worker again.
- A required service that is only **unavailable** never stops startup: the jobs that need it wait (see *Outages*), so a short outage while the server boots does not stop the worker.
- **Optional** (`optional = True`): a blank address turns the feature off (`DISABLED`), which never blocks anything:
  - Prowlarr off: automatic search is off. A submitted torrent is still downloaded, but no backfill search is scheduled (`jobs/submit_torrent_job.py`).
  - Sonarr off: Keep is off. The Keep button is disabled with the reason, and the API refuses a Keep.
  - Jellyfin or Shoko off: no library refresh or Shoko metadata.
- Anime-Lists has nothing to set up and is never contacted to check it, so it never stops startup; its jobs wait while it is down.

## Status for the dashboard and Keep

The worker keeps service health in memory and writes it to the `service_status` table (migration 062) on every change, and at least once a minute (`status_store.py`). The API runs in another process and reads it:

- `GET /api/v1/services` (dashboard session required) lists every service with its status, message, when it was checked, and `stale` when the worker has not written for 15 minutes. The dashboard shows this as a bar at the top of the page; hovering an item shows the full message.
- **Keep** needs Sonarr. While Sonarr's stored status is anything but `UP` or `UNKNOWN`, the job's Keep capability has `execution_available: false` with the message as `availability_reason`, so the button is disabled with a tooltip, and `POST …/keep` is refused: `503 SERVICE_UNAVAILABLE` while Sonarr is down, `409 KEEP_CONFIGURATION_INVALID` while it is off or not set up, with the message, for example `Sonarr is unavailable: HTTP 503 at http://sonarr:8989`. Nothing is queued. A stale or missing status never blocks: the job then decides. A Keep whose import already finished and only resumes its cleanup or Shoko follow-up does not need Sonarr and is never blocked.

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

A provider is `DOWN` while a cooldown after failed requests is active, `UNKNOWN` when that cooldown has ended (the next real request decides), and `UP` with the age of its newest cached data otherwise.

### The startup check for AniDB and TVDB

A client name or API key being set does not prove it is accepted, so the worker's startup check (`startup_probe`) does test them:

- **TVDB:** one login. TVDB has no ban risk; `401`/`403` means the key was rejected.
- **AniDB:** at most one `anime` request for a fixed title, sent through the same pacing as real requests (`Clients.anidb_check` in `providers/http.py`):
  1. If an AniDB anime request succeeded in the last 24 hours, nothing is sent: that already proves the setup.
  2. If a cooldown after failed requests is active, nothing is sent. The verdict the last worker saved in `service_status` is kept, so a rejected client stays rejected across restarts.
  3. Otherwise one request is sent and cached for 24 hours like any other. AniDB reports problems with HTTP 200 and an `<error>` document: an error that mentions the client means the client was **rejected** (startup stops); `Banned` or no answer means **unavailable** (startup continues, jobs wait). Every failure starts the usual cooldown (10 minutes or more).

The regular checks after startup still never contact AniDB, Anime-Lists or TVDB.

## Jobs declare the services they need

Each job registration lists the services its handler contacts:

```python
KIND='SUBMIT_BATCH'
SERVICES=('qbittorrent',)  # External services this job needs; see services/README.md.
...
return Registration(KIND, DEFINITION, execute, services=SERVICES, ...)
```

The worker **does not claim** a job while any service it needs is blocking: `DOWN`, `NOT_CONFIGURED`, `AUTH_FAILED` or `UNSUPPORTED`. The job stays `PENDING` or `RETRY_WAIT` and spends no retries. Local-only jobs declare nothing and keep running during an outage. If a job names a service that has no adapter, the worker refuses to start.

| Service | Job kinds |
|---|---|
| qBittorrent | `PREPARE_ACQUISITION`, `SUBMIT_TORRENT`, `DOWNLOAD_TORRENT`, `SUBMIT_BATCH`, `TRANSFER_BATCH`, `UNLOCK_BATCH`, `CLEANUP_BATCH_DOWNLOAD`, `CLEANUP_CANCELED_OPERATION`, `CLEANUP_DECIDED_DOWNLOAD` |
| Prowlarr | `DISCOVER_EPISODE_RELEASES` |
| Sonarr | `KEEP_MEDIA`, `KEEP_BATCH`, `CLEANUP_KEPT_MEDIA`, `CLEANUP_BATCH_KEPT_MEDIA` |
| Jellyfin | `REFRESH_JELLYFIN` |
| Shoko | `ENRICH_SHOKO_METADATA` |

AniDB, TVDB and Anime-Lists are not declared yet. Identification and verification already wait for them through provider pacing (`ProviderDeferred`).

What the worker does:

- **At startup** it checks every service once (see *Required and optional services*), saves the result for the API, and refuses to start if a required service is misconfigured.
- **Before each claim** it rechecks services whose time has come: every 60 s while up, and with 15 s to 5 min backoff while down. Concurrent attempts never probe the same service twice. When the settings or credentials revision changes, every service is rechecked at once, so a fixed password does not wait out the backoff.
- **After each job** it applies passive marking (below).
- It logs `SERVICE_DOWN` and `SERVICE_RESTORED` once per change, with `service=`, `service_status=` and a `service_detail=` text such as `Prowlarr is unavailable: HTTP 503 at …`.

## Passive marking

When a real request fails, the service is marked at once, without an extra network call:

- **From a job's result:** a job reports a service problem with a generic code and names the service in the outcome details (`service_facts('qbittorrent', ...)`). Codes never name a product, so a service can be replaced without renaming codes:
  - `SERVICE_UNAVAILABLE` marks the named service `DOWN`;
  - `SERVICE_SETUP_INVALID` marks it with the `service_status` in the details (`NOT_CONFIGURED`, `AUTH_FAILED` or `UNSUPPORTED`);
  - `SERVICE_RESPONSE_INVALID` (an unreadable answer) does not change its status.

  Only a service the outcome **names**, and the job declares, is marked. The service is never guessed: a job that declares Prowlarr also calls AniDB and TVDB, and their errors must not mark Prowlarr. Adapters also map a few role codes (`reasons`; for example Prowlarr: `RELEASE_SEARCH_TIMEOUT` → `DOWN`). Other jobs that need a marked service then wait instead of failing. A job that completes marks its services `UP`.
- **From an exception:** `ServiceHealth.mark_failed(service, exc)` uses the adapter's `classify(exc)` to turn the client's own exception into a status.

Expected waits, such as a planned AniDB pacing delay, return `NOT_A_FAILURE` and never mark a service down. Each status change is returned once as a `Transition`, so it is logged once instead of on every attempt.

## Outages: waiting, not failing

When a job hits an unavailable service, it **waits** with the reason `SERVICE_UNAVAILABLE` instead of failing:

- **What converts:** a `retry` with `SERVICE_UNAVAILABLE` that names one of the job's services, a `retry` whose role code the adapter maps to `DOWN`, or an uncaught client error that the adapter's `classify()` reports as `DOWN`. The job is deferred for `Policy.service_wait_ms` (60 s) and **spends no retry**. While the service stays down, claim gating keeps it waiting.
- **What the history shows:**
  - `service` (for example `prowlarr`);
  - `service_detail` (`Prowlarr is unavailable: HTTP 503 at …`), which the dashboard and `adm report` display;
  - for a role code such as `RELEASE_SEARCH_TIMEOUT`, that code as `cause_reason_code`;
  - any `failed_check`;
  - `waiting_since_ms`.
- **Giving up:** once a job has kept running into the outage for `Policy.service_give_up_ms` (**72 hours**), it goes to `NEEDS_REVIEW` with `SERVICE_UNAVAILABLE`. The clock counts the current run of service waits, and a manual retry or any unrelated outcome starts a new run. `adm recover` treats `SERVICE_UNAVAILABLE` as safe to retry once the service is back.
- **What does not convert:** configuration and credential problems (`SERVICE_SETUP_INVALID`, with status `NOT_CONFIGURED`, `AUTH_FAILED` or `UNSUPPORTED`) stop for review, because waiting would not fix them. History names the service, for example "Prowlarr rejected the login or API key." A `SERVICE_UNAVAILABLE` that names no service (an AniDB or TVDB outage inside a Prowlarr job) is an ordinary retry. The startup check catches them for required services. Codes that also cover local disk failures, such as `DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE`, are not mapped to a service, so a disk problem is never reported as an outage.

## Adding a service

1. Create `adapters/<name>.py` with a subclass of `ServiceAdapter`:

   ```python
   from src.anime.services.base import DOWN, NOT_CONFIGURED, UP, ServiceAdapter

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

   For an optional HTTP service with an address setting, an API key and a status endpoint, subclass `KeyedServiceAdapter` from `adapters/_http.py` and set `status_path`, `key_getter`, `key_header`, `version_keys` and `off_detail`, as Prowlarr, Sonarr, Jellyfin and Shoko do. Set `required = True` only if the worker must not start without it, and override `startup_probe()` if the regular probe cannot prove the setup works.
2. Add the class to `BUILTIN` in `adapters/__init__.py`. External plugins can call `ServiceRegistry.register(adapter)` instead.
3. Add tests that use a fake transport, never a real service (see `tests/test_service_adapters.py`).

## Roadmap

1. **Done:** adapters, registry, monitor and `adm services check`.
2. **Done:** jobs declare which services they need, the worker skips jobs whose services are down, and job results mark services passively. This replaced `QbittorrentReadiness` and `ACQUISITION_KINDS`.
3. **Done:** an outage means waiting, not failing, with `SERVICE_UNAVAILABLE` naming the service and a 72-hour give-up. The per-service codes were retired in refactor RF-006c (migration 065): handlers report the generic codes with the service in the details. Episode and season-pack discovery now wait through a Prowlarr outage like every other job. Timeouts (`RELEASE_SEARCH_TIMEOUT`) keep their one delayed retry, then a manual Retry.
4. **Done:** the worker does not start while qBittorrent, AniDB or TVDB is misconfigured (exit 78, no restart loop); Prowlarr and Sonarr are optional; the dashboard shows a status bar, and Keep is refused with the reason while Sonarr cannot work.
5. **Later:** declare AniDB, TVDB and Anime-Lists on the identification and verification jobs, so they also wait through claim gating.
