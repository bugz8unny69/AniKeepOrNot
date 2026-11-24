# Plan: one job per series, a Running page, and the rename to AKON

Status: plan, for the owner to read and correct. Written 2026-11-22.
Nothing here is built yet.

## Decided by the owner

- **One job per series**, keyed by AniDB ID. It never ends until you press Keep or Reject.
- **N torrents at a time per series,** set on the dashboard (Settings).
- **The job sleeps** when there is nothing to do, and the API wakes it: "these releases belong to you, work on them".
- **No job numbers on the dashboard or in `adm`.** The Jobs page goes away. Intake shows each series and what is happening with each episode.
- **A Running page** shows what is being worked on right now.
- **The API logs what it does,** not only the worker.
- **A one-time cutover** stops the services, converts everything to the new format, renames `anime-discovery` to `akon`, and brings everything back up. Work in progress is converted in place; nothing is downloaded again.
- **The new name is `akon`.** The web address moves to `/akon/`, and the old `/anime-discovery/` address keeps working for a while.
- **History is abandoned:** the old job history is wiped at the cutover, not converted.
- **qBittorrent's category and folders are renamed to `akon` too.**

## 0. One series, one row (owner, 2026-11-23)

The database must grow with the number of series, not with the work done.
Today 138 series take 11,680 jobs and 515,766 history rows; history and its
indexes are 86 MB of the 140 MB database, spread over 56 tables that point at
step job numbers.

The series job is **one table, one row per AniDB ID**:

```sql
CREATE TABLE series (
 anidb_id INTEGER PRIMARY KEY,          -- the series; also its ID everywhere (adm, dashboard)
 title TEXT NOT NULL,
 state TEXT NOT NULL,                   -- WORKING, ASLEEP, KEPT, REJECTED
 wake_at_ms INTEGER,                    -- asleep until then (a download check); NULL = until woken
 torrents_at_once INTEGER NOT NULL DEFAULT 1,
 torrents_json TEXT NOT NULL,           -- each torrent: hash, name, step, state, why, episodes
 episodes_json TEXT NOT NULL,           -- each episode: state, which torrent, file
 recent_json TEXT NOT NULL,             -- the last 50 history lines, for the dashboard
 lease_token TEXT, lease_expires_at_ms INTEGER,
 created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
```

- **No step rows.** A step updates its torrent inside the series row.
- **History is bounded:** the row keeps only the last 50 lines; the full story is in the log.
- **8,000 series means 8,000 rows,** a few KB each.
- **Kept:** the provider cache, the identity queue (pruned daily) and notifications. They don't grow with steps.
- **Removed at the cutover:** jobs, job history, job operations, and the per-step tables.

## Branches (owner, 2026-11-23)

- **`develop`** stays what runs on production, for short fixes.
- **`feat/series-job`** holds this rework, the cutover and the rename, until it is ready.


- **One per AniDB ID.** A separately listed OVA, movie or special on AniDB is its own entry, with its own ID. A TVDB season 00 release goes to the AniDB entry the anime-lists map gives for it. Specials that AniDB lists inside a series (S1, S2, …) are episodes of that series.
- **States:**
  - **Working:** it is on a step.
  - **Asleep:** it is waiting for a release, a download, or your decision. It uses no worker time and runs no polling checks.
  - **Decided:** Keep (Sonarr owns it from then on) or Reject (everything removed). The job ends.
- **Woken by:**
  - the API, when it identifies a release for that AniDB ID;
  - qBittorrent, when a download it is waiting on finishes (one shared check for all downloads, not one per job);
  - your actions (Keep, Reject, Search, Retry).
- **Its torrents, in order:** each one recorded by release name, with its state and why.
- **N torrents at a time,** set on the dashboard (Settings, "Torrents worked on at once per series"; default 1). Each torrent goes through the steps inside the job: check the release, download, verify, transcode, stage. A torrent that can't be used is dropped with the reason, its files are cleaned up, and the job takes the next torrent.
- **Two torrents never work on the same episode at once.** The job reserves each episode for one torrent, as today.
- **One history per series.** Every line names the episode and the torrent, for example "S02E03 [Group] … : downloading, 53%".
- **Errors:** an expected problem (a service down, a bad torrent) is handled by the job. An unexpected error is a program bug: the full stack trace goes in the log, the torrent stops with "program error", and you are told (work items 1–3).

## 2. Dashboard

- **Intake** lists the series. Open one to see each episode: its torrent, its step, and what happened.
- **Running** (replaces Jobs) lists what is happening now:
  - each series being worked on, its episode, its step and its progress;
  - downloads in qBittorrent, with progress;
  - releases the API is identifying (waiting on AniDB).
- **Removed:** the Jobs page, and every link or button that takes a job number.

## 3. `adm` commands

- `adm report intake --id <anidb_id>`: the series, its torrents, its episodes and its history.
- `adm report job --id` is removed.
- Keep, Reject, Search, Retry and Cancel take an AniDB ID.
- `adm running`: the same as the Running page.

## 4. Logs

**The API logs every step it takes,** for example:

```
API  POST /check  [Group] Show - 05  new release, identifying with AniDB (503, ask again)
API  identity     [Group] Show - 05  identified: AniDB 16999 Show
API  POST /check  [Group] Show - 05  accepted (200); woke the worker for series 16999 Show
API  POST /check  [Group] Movie      rejected (422): movie
```

**The worker** logs one line when it starts on a series, then one line per step started, finished or failed, each naming the series, the episode and the torrent. A repeated wait logs nothing (work item 4).

## 5. The one-time cutover

A script in `fixes/`, tested first on a copy of the production database (on production, in a temporary folder).

1. **Stop** the services and take maintenance. Autobrr's calls get 503 meanwhile, so Autobrr retries them, and its retries run out quickly (2.5 minutes). Do the cutover when few releases arrive, or switch off AKON Check during it.
2. **Convert the work.** Today production has 81 unfinished jobs, all on hold or in Needs review: 40 episode searches, 18 user evaluations, 15 episode reservations, 7 batch reservations, 1 batch keep. Each series with unfinished work becomes one series job. Its torrents and episodes keep their state, and it resumes from where it was.
3. **Wipe the history.** The old job history (about 513,000 rows) is deleted, not converted. Each series job starts with a fresh history.
4. **Remove the old tables** once nothing uses them: jobs, job history, job operations, and the per-kind tables replaced by the series job. No stale tables remain.
5. **Rename:**

   | Today | After |
   |---|---|
   | `anime-discovery-api.service`, `anime-discovery-worker.service`, `anime-discovery.target` | `akon-api.service`, `akon-worker.service`, `akon.target` |
   | user and group `anime-discovery` | `akon` |
   | `/etc/anime-discovery` | `/etc/akon` |
   | `/var/lib/anime-discovery/anime-discovery-develop.sqlite` | `/var/lib/akon/akon.sqlite` |
   | `/opt/anime-discovery` | `/opt/akon` |
   | `https://…/anime-discovery/api/v1/…` | `https://…/akon/api/v1/…`; the old address is still passed through to AKON |
   | Logger names, log lines, User-Agent `AnimeDiscoveryNextGen` | `akon` |

   | qBittorrent category `anime-discovery` and its save folders | category `akon`, folders renamed to `akon` |

   The installer (`akon-install`) learns the new names, and the old ones are removed. Torrents in qBittorrent are moved to the new category and folders through qBittorrent itself (it moves the files and keeps seeding), and the database's saved paths are updated to match.
6. **Start** the services, check the log and the Running page, then point Autobrr at `/akon/api/v1/…`. The old address can be retired later.

## Build order

1. **API logging** (section 4). Small, and useful straight away.
2. **The series job, the Running page and the `adm` commands,** behind a setting, tested on production with a copy of the database.
3. **The cutover script and the rename,** tested on a production copy, then run once.

## Answered by the owner (2026-11-23)

- **Torrents at a time per series:** N, set on the dashboard.
- **Old history:** abandoned; wiped at the cutover.
- **qBittorrent category and folders:** renamed to `akon`.
