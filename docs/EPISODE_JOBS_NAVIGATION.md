# Episode-aware Jobs navigation (NextGen)

## View Series → Jobs

View Series keeps its existing single dialog. Each staged or missing episode shows
its historical reservation count and a **Related jobs** action. The action navigates
to the existing Jobs table with the URL-fragment fields `anidb_id`, `season`, and
`episode`; no second modal or extra page is introduced.

These fields select verified **TVDB season/episode placements**, never values
parsed from a release title. The authenticated API lists all jobs in operations
owning matching single or batch reservations, including released reservations.
It also includes related Sonarr backfill discovery jobs where a stored episode
was observed or exhausted. Other jobs, such as an initial identity job with no
verified episode or reservation yet, may not be linked until evidence exists.
The public dashboard shell intentionally rejects URL query strings; the\nJobs fragment is parsed by the browser and transferred to authenticated API\nquery parameters. The filtered listing uses the same pagination, search, status filters and access
controls as Jobs. It initially shows completed and canceled jobs as well.

Use the browser's Back button. The series and selected episode are retained
in the prior history entry as `/#series=ID&episode=SxxExx` so reopening Intake
can restore the series dialog and scroll toward that episode. Closing the
dialog clears the URL marker; there is no dedicated Back to Series button.

## Jobs display

The left title cell prioritizes the title selected by the existing
`anime.title_language` setting (`japanese` or `english`) using the shared
`preferred_title` fallback. It shows the verified TVDB episode placement
independently from the torrent's original release name. Multiple exact
placements are summarized compactly. An unverified episode is explicitly
marked "Episode not determined" rather than guessed from `E05` or Japanese
text in a torrent filename.

The original release name is unchanged, truncated to one line with a full-name
native tooltip. A "Release details" expander reveals the complete raw title
and known TVDB placements inline. Only the most recently expanded job is kept
expanded across the five-second polling refresh. The expander is not a new
job status or a replacement for Matching Details / History.

## Manual submission

Manual candidate submission was implemented in the follow-up:
[Manual episode submissions](MANUAL_EPISODE_SUBMISSIONS.md).
It creates independent acquisition jobs and leaves the exclusive reservation
constraint intact. View Series shows manual submission counts and the existing
Jobs table lists candidate operations. This document's navigation and release
display changes remain unchanged.

## Offline tests

Added cases cover exact reservation filtering (including negative cases),
literal/full release names, target labels and the View Series navigation
action. They are authored but not run; no GitHub Actions workflow was invoked.
