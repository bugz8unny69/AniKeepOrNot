# One job, one duty, many activities

Status: **design, awaiting owner approval**. No code changes yet.
Branch: `refactor`. The owner's rule is that NextGen keeps only fixes, and behaviour
changes that improve things land on `refactor` and are listed in the PR.

## What the owner wants

> The worker is the one who directs, depending on what it gets from the API.
> When the worker gets a job from the API, it assigns a duty. That job is
> responsible for the whole lifecycle, to get things done. The parts inside it
> are activities: "I am identifying", and so on. Each step of the one job
> explains what it is doing.

> Intake needs to know, when it identifies, whether to reject. Identification
> never needs review.

## Terms

| Term | Meaning |
|---|---|
| **Intake** | Accepts what arrives through the API and identifies it. It either hands the work to the worker or **rejects** it. It never sends anything to review. |
| **Worker** | The director. It takes what arrives from the API, decides the duty, gives it to a job and runs that job. |
| **Job** | One job per piece of work from the API. It owns that work from start to finish. It does not launch other jobs. |
| **Duty** | What the job has to get done, assigned once by the worker: `EPISODE`, `SEASON_PACK`, `BATCH`, plus maintenance duties such as `RESET_EPISODE` and `CLEANUP`. |
| **Activity** | One step of the job's lifecycle, e.g. *Identifying*, *Verifying release*, *Downloading 42%*, *Staging*. The job always shows its current activity and what it is doing. |

## Why

Today one release becomes a chain of a dozen or more jobs:
- Identify, Verify, Reserve, Prepare, Submit and Download;
- Verify media, Stage and Keep;
- Cleanup and Reset.

Each job knows only the job that created it. Batches have their own chain of
nine kinds, and whether the work is an episode or a pack is only discovered
partway down the chain. When something stops, you have to find which of
the many jobs stopped and why. Job #7362 got stuck because a reset had to
rediscover the cleanup jobs it had launched.

## How it works

### 1. The API stops known rejections before any job

The webhook is received by the **API process**. Before it creates anything,
it checks the release name against **local memory only**: the recognized
release names (`recognized_for_release`, seeded by earlier verified
releases) and your rejections (`anime_review_decisions`). It makes no AniDB,
TVDB or Prowlarr call, so the webhook stays fast.

| API check | What happens |
|---|---|
| Name recognized as a series you rejected | Release recorded as **rejected** (`ANIME_REJECTED`) in its History. The rejected series' count goes up. **No job is created.** Response: `200 {"rejected": true, "reason_code": "ANIME_REJECTED"}` |
| Name not recognized, or a series not rejected | Accepted as today and handed to intake (`202`) |

* A series you rejected was identified and verified before you rejected it,
  so its release names are normally already recognized. The API stops it on
  the very first re-send.
* A re-send under a name the API doesn't recognize (a new group or a
  different title) goes to intake. Intake identifies it and rejects it, still
  with no job and no review.
* The response is a `200` with `rejected: true`, not an error, so autobrr
  doesn't log the webhook as a failure. If autobrr is set up to call the webhook as
  an external filter, autobrr can then skip the download on its side too
  (to confirm with the owner's autobrr setup).

### 1b. Intake identifies, then accepts or rejects

Identification happens **at intake, before any job exists**. Intake decides
on the spot:

| Intake result | What happens |
|---|---|
| Identified: series known, release type supported | handed to the worker |
| Not identifiable (no AniDB/TVDB match, ambiguous match) | **rejected**, with the reason (`IDENTITY_INCOMPLETE`) |
| Conflicting identity (AniDB and TVDB disagree) | **rejected**, with the reason (`IDENTITY_CONFLICT`) |
| Unsupported release type (movie, specials) | **rejected**, with the reason (`RELEASE_TYPE_UNSUPPORTED`) |
| Series already rejected by you (e.g. autobrr sends it again) | **rejected** (`ANIME_REJECTED`). No job is created |
| An identity service is down (AniDB, TVDB) | **waits** for the service (service health layer), then identifies; this is not a rejection |

* **Repeat sends of a rejected series.** Autobrr can keep sending releases of
  a series you rejected. Each one is identified at intake, matched to your
  rejection and rejected: no job, no hold and no review. The Intake page
  doesn't list each one separately. The rejected series shows a count and the
  last time, e.g. "rejected · autobrr sent 7 more releases, last 18:40".
  Each one is still recorded in that release's History.

  Today the same re-send runs Identify, Verify release and Select as jobs,
  then stops `ON_HOLD · ANIME_REJECTED` at Reserve (or Evaluate/Reserve for
  batches), leaving held jobs behind on every send.
* Intake **never** produces Needs review. Today `IDENTITY_INCOMPLETE` and
  `IDENTITY_CONFLICT` go to Needs review; on `refactor` they become intake
  rejections (a behaviour change, to be listed in the PR).
* A rejected intake is visible on the Intake page with its reason. The
  manual-identity action stays: entering a match there re-submits the release
  to intake, which identifies it again and either accepts or rejects it.
* Only an accepted, identified release reaches the worker, so every job knows
  its series from its first activity.

### 2. The worker directs

For each thing intake accepts (a release, or a retry, reset, Keep or Reject
for an existing series), the worker:

1. creates **one job** for it;
2. assigns the **duty**. For a release, that means deciding episode, season pack
   or batch, once and up front, using today's rules moved into one place;
3. runs the job, concurrently with other jobs, within its concurrency limit.

### 3. The job carries the duty through its lifecycle

The job runs its duty's activities in order. The activities are today's job kinds,
reused as steps with the same code, checks and atomic commits:

```
EPISODE      Verifying release → Selecting → Reserving → Preparing
             → Submitting → Downloading → Verifying media → Staging
             → Waiting for your evaluation → Keeping | Rejecting → Cleaning up
SEASON_PACK  Evaluating pack → Reserving → Submitting → Transferring
 / BATCH     → Verifying probe → Verifying episodes (n of N) → Staging
             → Keeping → Unlocking → Cleaning up
RESET_EPISODE  Cancelling owned work → Cleaning up → Searching again
```

* After each activity the job saves where it is. If the worker restarts, the
  job continues from that activity.
* A waiting activity (a download in progress, a service outage) gives the
  worker its slot back until its next check. Nothing else waits on it.
* The job **never launches another job**. Work that is a separate job today,
  such as cleanup during a reset, becomes an activity of the same job.

### 4. Each activity says what it is doing

The job's current activity and its own words are always visible:

```
Job #900 · Episode · Frieren S01E03 (AniDB 17617)         NEEDS REVIEW
  ✓ Verifying release      [SubsPlease] Frieren - 03 (1080p)
  ✓ Downloading            done, 1.4 GB
  ✗ Cleaning up            stopped at cancellation_service.py:… TORRENT_MOVED
                           file /downloads/…                   [Resume] [Reset]

Job #901 · Season pack · Frieren S02 (12 episodes)        ACTIVE
  ✓ Evaluating pack        12 of 12 episodes covered
  ▶ Verifying episodes     episode 7 of 12
```

`jobs.activity_code` and the job diagnostics ("doing", "waiting for")
already exist. They become the activity list above, one line per activity,
recorded in History as the job goes.

### 5. Jobs of the same series stay together

A second release for the same series is its own job with its own duty. It is
**attached to the series**: the dashboard shows the series' jobs together,
and the worker keeps two jobs of one series from working on the same episode
or torrent at once (today's reservations, with one clear owner).

### 6. Stops, retries and resets

* A stopped activity stops **its job only**. Other jobs keep running, and so
  does the worker. An unexpected error in one job sends that job to review; it
  does not stop the worker.
* **Resume** continues the job from the activity that stopped, recording
  who asked. A cleanup that stopped to protect files it doesn't own is resumed
  only by an explicit Resume.
* **Reset** of an episode is a `RESET_EPISODE` job: its own activities clean up
  and search again. It doesn't depend on other jobs.

## What changes in the code

| Area | Change |
|---|---|
| handlers in `jobs/` | each becomes an **activity function** (same code), callable from the job's lifecycle |
| new `duties.py` | each duty's ordered activities, plus the worker's duty decision |
| API `/webhook` (`api/app.py`) | local check (recognized names and rejections) before `accept_event_and_job`; a rejected series gets no job |
| intake (`storage/series_intake.py`, identify step) | identifies before any job; identity failures become rejections; outages wait |
| `worker.py` | creates the job, assigns the duty, runs activities, saves progress after each one |
| successor enqueue in handlers | replaced by "next activity"; no new jobs |
| `jobs` table | `duty` and `activity_code` (exists); job kinds become duties |
| History | one job's History shows every activity, with what it did |
| dashboard and `adm report job` | the activity list above; jobs grouped by series |

## Phases (each a PR, tests local only)

0. **JD-0 Reject before any job.** The API stops recognized rejected series
   at the webhook, with no job and no provider call. Intake identifies the rest; identity failures
   become rejections and never go to review; outages wait; re-sends of a
   rejected series are rejected without a job. Test: each row of the intake
   table above, and the same rejected series sent five times (no job, one
   series entry, count 5). Also: a webhook for a recognized rejected
   series creates no `jobs` row and makes no provider call.
1. **JD-1 Activities.** Each handler's work becomes an activity function, called by
   today's job wrappers. No behaviour change yet.
2. **JD-2 Episode duty.** The worker assigns `EPISODE`, and one job runs every
   activity. Test: one release end to end in a real worker as **one job**, the
   activity list, a restart mid-download, and one job stopping while another
   finishes.
3. **JD-3 Season-pack and batch duties.**
4. **JD-4 Reset and cleanup duties.** No cross-job waiting.
5. **JD-5 Switch-over.** All new work arrives as duties.
6. **JD-6 Dashboard and report.** The activity list, and jobs grouped by series.
   The old kinds are removed (ledger entries).

## Decisions for the owner

1. **In-flight work at switch-over.** Recommended: old job chains finish
   the old way, and new work starts as duty jobs.
2. **Duty rules.** Recommended: today's rules for episode, season pack and
   batch, moved into the worker, with no new rules yet.
3. **Re-identifying a rejected release.** Recommended: a manual match on the
   Intake page re-submits it to intake. Alternative: rejected stays rejected,
   and a new release is needed.
4. **Resume scope.** Recommended: Resume one job. The series view also offers
   "Resume all stopped jobs of this series".
