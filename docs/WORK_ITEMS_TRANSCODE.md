# Transcode work items

Based on the user's `/home/lutchyh/Downloads/work-items-transcode.md`.
The existing uncommitted T1 work came from the sibling `anime-discovery-refactor`
worktree at c358cd2e. Its original files have been preserved there.

| Item | Implementation |
|---|---|
| T1 | Standalone Linux agent, shared VAAPI/x265 encoder, scoped temp cleanup, authenticated streamed HTTP, cancellation, service/env files and setup docs. |
| T2 | Off/Local/Remote settings, ordered agents and private tokens, audio-aware bitrate budget, persisted attempt limits, waits/failover, pipeline staging and service health. |
| T3 | Queue-and-exit `adm transcode staged/status`, one resumable batch row, shared slots with pipeline priority, atomic replacement and staging/resource updates, Jobs summary and completion notification. |

The existing worker, Publisher, safe path helpers, resources interface, Retry,
configuration and secrets stores, service adapters, and notification interface
are reused. There is one encoder implementation. No deployed services or user
media/databases were changed during development.

Database migrations: 072 (transcode media/probe details), 073 (staged batches and
notification type). Configuration schema: 17. Secrets schema: 9 (older schemas
remain readable; tokens are never returned by the API).

Validation: the full local suite passed (2,034 tests and 6,391 subtests); the
focused transcode suite passed all 30 tests after the final staging changes;
all 33 dashboard tests passed. See [setup and operation](TRANSCODE_AGENT.md).
