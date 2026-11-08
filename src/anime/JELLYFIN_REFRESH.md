# Jellyfin library scan follow-up

After successful staging, Keep or post-Keep staging cleanup, a configured Jellyfin address queues an
independent REFRESH_JELLYFIN job in the same SQLite completion transaction.
Staging and Keep finish independently of scan success. No extra video readback,
checksum, file copy or deletion is introduced. Shoko remains optional.

Set jellyfin.base_url (HTTP(S), no base-path support) and timeout_seconds in
Settings. Replace the API key through PATCH /api/v1/credentials/jellyfin or the
Jellyfin credential form. Credential reads expose only jellyfin_configured.
The key is stored privately with existing credentials; secrets versions 1–4
remain readable and a changed Jellyfin key writes version 5. Configuration v9
requires the existing explicit upgrade procedure; SQLite remains v22.

Add the staging and permanent roots to Jellyfin libraries beforehand. The
worker POSTs /Library/Refresh with X-Emby-Token and accepts 204. This requests a
server-wide library scan, not an item-specific metadata overwrite. API contract:
https://github.com/jellyfin/jellyfin-apiclient-python/blob/master/jellyfin_apiclient_python/api.py
A completed refresh job confirms request acceptance only: it does not prove
that Jellyfin has indexed or correctly identified an episode.

The address is blank by default, disabling automatic follow-up creation.
Already completed staging/Keep jobs are not backfilled. Existing refresh jobs
use current settings and credentials on each attempt; clearing the address puts
an existing job into review rather than silently declaring a scan successful.
Network/server errors use the normal bounded worker retry policy. Authentication
or unexpected responses stop for review and show catalog explanations in Jobs.
The existing stopped-job recovery tooling applies; a new refresh-specific retry
button is not introduced. A crash or lost response can cause a scan request to
repeat; this is acceptable for this non-destructive follow-up. Redirects and
ambient HTTP proxies are disabled; no live request happens during composition.

Offline cases are authored but unrun under current policy. Source syntax and
whitespace checks do not establish runtime, dashboard or server compatibility.
No live Jellyfin call, test execution, CI, migration or deployment was performed.
