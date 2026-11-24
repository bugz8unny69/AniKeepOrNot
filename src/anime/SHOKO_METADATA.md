# Shoko: link every kept file to its episode

**Owner, 2026-11-23.** Jellyfin gets its metadata through Shoko (Shokofin), and
it only shows files Shoko has linked to an episode. Shoko's own hash match often
fails, so **every Keep** (single episode or season pack) queues `LINK_SHOKO`
(`jobs/link_shoko_job.py`) for the series. It uses Shoko v5.3 API v3:

1. `POST Series/AniDB/{aid}/Refresh?createSeriesEntry=true`: Shoko fetches the series.
2. `GET File/PathEndsWith?path=<library-relative path>`: Shoko's record of the file.
   Not found: `GET ImportFolder/{id}/Scan` for each import folder, and check again in 10 minutes.
3. `GET Episode/AniDB/{eid}/Episode`: Shoko's episode ID for the AniDB episode ID.
4. `POST File/{fileID}/Link {"EpisodeIDs": [id]}`.

A file already linked to its episode is left alone, so the job can run any number
of times. After a day of waiting it fails (`MEDIA_LINK_INCOMPLETE`) and the log
lists the files. For a series kept before this existed: `adm shoko link --id <anidb_id>`.

The section below describes the older read-only title lookup, which Keep no longer queues.

# Optional Shoko metadata

Configured Keep completions enqueue ENRICH_SHOKO_METADATA in a separate control
operation. Blank `shoko.base_url` disables new follow-ups. The job reads the saved
AniDB anime and episode IDs from the completed Keep selection, then uses Shoko
v5 API v3 `GET Series/AniDB/{id}` and `GET Episode/AniDB/{id}`. The `apikey`
credential is sent in a header, never in a URL. Missing catalog entries need review;
network/429/server failures use ordinary bounded retries. No Shoko import-folder,
scan, hash, file-link, move or rename endpoint is invoked.

Only exact matching ID responses and bounded plain titles are retained in the
existing keep_imports JSON journal. They are separate display metadata. Shoko
results never modify AniDB identity, verified TVDB placement, existing NFOs,
Sonarr imports, cleanup eligibility or kept-episode protection. Keep completion
and all cleanup work remain independent of Shoko failure. This does not register
files in Shoko: configure and operate Shoko's own catalog separately if desired.

Management receipts expose `optional_shoko`; the dashboard displays result/status
and offers Retry Shoko metadata for a stopped review/blocked job. The existing
Keep action resumes only that job after any higher-priority stopped cleanup.
It never reimports the episode. Existing completed Keeps are not backfilled.

Configuration v10 adds the blank optional address and a 1–60 second timeout,
with an explicit v9→v10 preview/backup/apply conversion. Historical converter
shapes remain free of newer fields. Secrets v6 adds the private Shoko API key;
v1–v5 remain readable. PATCH `/api/v1/credentials/shoko` uses the existing
management authentication, expected revision and session/CSRF contract. Public
credential results expose only `shoko_configured`. SQLite remains v23; no new
SQL or migration was added.

Primary API contracts inspected at ShokoServer v5.0.0:

- [Series controller](https://github.com/ShokoAnime/ShokoServer/blob/v5.0.0/Shoko.Server/API/v3/Controllers/SeriesController.cs)
- [Episode controller](https://github.com/ShokoAnime/ShokoServer/blob/v5.0.0/Shoko.Server/API/v3/Controllers/EpisodeController.cs)
- [Authentication handler](https://github.com/ShokoAnime/ShokoServer/blob/v5.0.0/Shoko.Server/API/Authentication/CustomAuthHandler.cs)
- [Series model](https://github.com/ShokoAnime/ShokoServer/blob/v5.0.0/Shoko.Server/API/v3/Models/Shoko/Series.cs)
- [Episode model](https://github.com/ShokoAnime/ShokoServer/blob/v5.0.0/Shoko.Server/API/v3/Models/Shoko/Episode.cs)

Offline transport, strict response/identity checks, historical config/secret
compatibility, auth separation, nonblocking Keep and retry-without-reimport cases
are authored but unrun. No live Shoko request, file scan, hash or media operation
was executed. Compatibility with the installed lab version must be verified.
