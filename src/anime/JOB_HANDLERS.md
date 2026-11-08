# Job handler module naming

A Python module that provides a job handler called by the worker MUST use a
lowercase snake_case name ending in `_job.py`. Use an action-oriented stem so
its purpose is clear when browsing the filesystem.

Examples of future module names:

| Purpose | Handler module | Offline test module |
| --- | --- | --- |
| Identify an anime | `identify_anime_job.py` | `test_identify_anime_job.py` |
| Acquire a release | `acquire_release_job.py` | `test_acquire_release_job.py` |

These are naming examples, not implemented handlers or authorization to build
provider/acquisition integrations. The eventual handler package location and
callable input/outcome contract will be settled with the worker implementation.

The suffix identifies the worker-facing handler entry module. Shared provider,
storage, configuration, throttling and other helper libraries keep descriptive
ordinary module names; calling a helper from a job does not make it a job handler.
The long-running worker process/module itself is named `worker`, without the
job suffix. A `_job.py` file is an importable module, not necessarily a standalone
CLI script or a separate process.

Job kinds are stable explicit registry keys. The worker maps each registered
kind to its handler callable; it must not derive imports from filenames supplied
in a job payload or scan and execute every file ending in `_job.py`. Renaming a
file alone does not rename persisted job kinds. Registry/async handler behavior
remains future implementation work.

## Queued jobs and handler calls

Keep the existing `jobs` table and independent job model. There is no new
work_items table, to-do table, workflow parent/child graph or array of called
handlers on a job row. Intake durably records an incoming event and initial job;
the worker claims a job by kind, then calls trusted registered handlers.
A handler that creates independent queued work creates a normal jobs row with
optional created_by_job_id provenance, as already agreed.

A job can call multiple handlers, including repeated calls to the same handler.
The worker records each invocation separately in job_history:

| Field | Meaning |
| --- | --- |
| handler_code | Stable registered code identifying the handler, independent of its filename |
| handler_label | Static English label snapshot from trusted registration, at most 128 characters |
| invocation_id | Fresh 32-character lowercase hexadecimal identifier pairing a start and ending |

These fields are all NULL on ordinary job lifecycle entries and old history.
Each call records HANDLER_STARTED and then HANDLER_COMPLETED or HANDLER_FAILED.
All three markers have shared human-readable messages. HANDLER_FAILED requires
an existing registered reason, such as JOB_HANDLER_FAILED for an unexpected
exception. Do not invent raw exception messages as reasons. Completion of a
handler does not complete the queued job or authorize a downstream side effect.
The worker separately decides job transitions, retries and successor creation.

## Registration and translation

handlers.py supplies HandlerDefinition(code, label) and an immutable display
HandlerCatalog. The default catalog is empty: this foundation implements no
identity/acquisition handlers. Each future `_job.py` implementation must have
trusted static registration before it can be recorded by the writer. Codes are
ASCII uppercase letters/digits/underscores, starting with a letter, at most 64
characters; UNKNOWN is reserved for fallback. Labels cannot contain control
characters, markup or interpolation placeholders. Labels/registrations must
never come from payloads, settings values, filenames or provider responses.
The actual callable dispatch registry remains worker implementation work.

MessageCatalog(..., handlers=catalog) accepts translations for
`handler.CODE.label`, alongside existing event/reason keys. Its handler()
renderer uses current registered translations, English fallback, or a validated
recorded label if the handler was removed. Invalid historical metadata gets
"Unknown job handler". Runtime history logging emits the code, static label,
invocation ID and job/event/attempt correlation without raw payloads or secrets.

## Transaction and recovery contract

Use append_job_history(..., handlers=catalog, handler_code=...,
invocation_id=...) inside an explicit caller transaction. Normal lifecycle calls
need none of these additional arguments. Handler markers require worker
attribution, a RUNNING job and the current positive attempt number. The writer
never commits, dispatches code or changes a job state. It rejects unregistered
handlers, duplicate starts/endings, endings without starts, changed job/handler/
attempt identity and endings earlier than their start. Endings retain the
original readable label even if a later catalog changed it. A fresh ID is needed
for every invocation and retry; identifiers are not lease tokens.

The future worker MUST verify its unexpired lease token/ownership in the same
transaction before writing these markers or committing related mutations.
These history shape checks are not stale-worker fencing. Acquire maintenance
admission around the entire attempt. Persist HANDLER_STARTED before calling
external code, and log its receipt only after that transaction commits. Do not
hold a SQLite transaction open across async/provider work. Persist its ending
and related database mutations atomically, then log after commit. If persistence
fails, do not continue as though the start or completion was recorded.

A crash can leave a committed start with no ending. Display "No completion
recorded" rather than inventing success, failure or a still-running handler.
The future worker's fenced lease recovery decides what happens to the job;
this library does not retry handlers or synthesize endings. Durable history is
written regardless of logging level. A broken logging sink cannot undo it.

SQLite v8 migration 008 rebuilds only job_history, adding the three nullable
fields, their all-or-none shape constraint, an invocation lookup index and
partial unique start/end indexes. Old rows and unknown codes get NULL metadata;
IDs, existing details/activities/components, FKs and indexes are preserved.
The jobs table, incoming_events, AnimeSeries and configuration versions are
unchanged. Published 001–007 are immutable. This is not a worker loop or API.
