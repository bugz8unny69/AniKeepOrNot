> **Historical planning proposal preserved from PR #121.** The current contract is [Intake through staging](INTAKE_TO_STAGING.md), [episode selection](../EPISODE_SELECTION_POLICY.md) and [Keep](../KEEP_SONARR.md). Earlier copy/hash/search proposals below are historical, not current requirements. Staging moves without destination hashing; built-in search is out of scope.

# Anime Intake, Staging and Keep: purpose and terminology

Status: planning only. Naming decision recorded 2026-10-23.
This document records the agreed user-facing vocabulary. It does not implement
the dashboard, change acquisition behavior or rename the application.

See [the end-to-end system design](INTAKE_STAGING_KEEP.md) for process ownership,
phase transitions, recovery, action behavior and the implementation map.

## Agreed name and purpose

Use **Intake** as the dashboard section name. Use **Anime Intake** when a
standalone heading needs to explain what is being collected.

The agreed product flow is **Intake → Staging → Keep or Reject**.

- **Intake** receives incoming release information from sources such as Autobrr
  and begins identification and processing. It does not imply a commitment to
  watch or retain the anime.
- **Staging** prepares verified episodes for viewing and consideration. **Staged**
  means those episodes are available for the user's decision, with no automatic
  permanent import.
- **Keep** is the explicit decision to put approved content into the permanent
  library. Content is permanent only after that operation succeeds and the
  permanent copies are verified.
- **Reject** is the decision not to keep/follow the anime. Its exact cleanup and
  future-arrival blocking policy must be designed explicitly.

This refines the earlier broad use of Intake for everything before Keep.
Use **Staging** for the preparation step and **Staged** for its successful
milestone, rather than describing all watchable undecided content as Intake.
The three steps describe product responsibilities, not new database job states
or a settled dashboard page layout.

A webhook delivery, a processing job and an identified anime remain different
records. These names do not merge their identities or lifecycles.

## What the user should understand

An incoming release can be received from Autobrr, identified, checked and
downloaded under the configured selection policy. Verified episodes can then
be made available in the configured viewing folder and Jellyfin. The user can
consider the anime and choose Keep or Reject.

This is the intended product flow, not a statement that every step already
exists. Keep's permanent-library integration, publication and other unfinished
features retain their separate implementation work.

Keep is the decision to retain the approved content through the permanent-library
flow. Reject is a decision about the anime; Cancel stops unfinished acquisition
work. A section name does not make those actions interchangeable.

## Intended experience

1. Receive a release announcement from a configured source such as Autobrr.
   Receiving it does not prove anime identity or authorize a permanent import.
2. Identify the anime and exact episodes, then apply the configured episode
   selection and size policy. Search for missing selected episodes is separate
   unfinished work; a numeric selection window is not evidence they were found.
3. Acquire only the approved selection and show its progress. If work cannot
   continue, explain why and offer the actions that are safe for that state.
4. Verify the downloaded media, including required Japanese and English audio,
   before publishing readable copies and metadata to the configured staging folder.
   Make those copies available through Jellyfin without implying the user has
   chosen to retain the show.
5. Let the user watch and decide. Keep begins a separate permanent-library
   operation through Sonarr; Reject records the decision not to follow the anime.
   Incoming content must not bypass a recorded rejection.
6. Report the decision's progress and outcome accurately. Permanent import must
   be verified before declaring Keep complete. Cleanup has its own ownership,
   shared-use, retention and recovery rules.

The dashboard should answer three questions for each item: What is it? What is
happening? What can I do next? The API supplies durable job status, action
availability and readable diagnostic explanations; the dashboard presents them.
A queued request is not completed work. A failed refresh or notification should
not erase a verified video or trigger a second import.

Cancel remains available for unfinished acquisitions that are waiting, held,
blocked or need review, subject to the existing service safeguards. Canceling
running work coordinates with the worker before cleanup. Successful completed
staging is a different milestone from Keep; no acquisition Cancel is needed once
that operation has successfully staged. Explicit Reject behavior after staging
requires its own scoped design and must not be simulated with acquisition Cancel.

## Presentation rules

- Navigation/section: **Intake**.
- Standalone planning heading: **Anime Intake**.
- Proposed viewing-folder setting label: **Staging folder**.
- Preparation step: **Staging**; completed viewing milestone: **Staged**.
- Decision: **Keep** or **Reject**; permanent-library success follows verified Keep completion.
- An item's status explains what is happening; Intake is not a job status.
  Examples include Downloading, Ready to watch and Needs attention. Exact
  mappings and readiness requirements remain part of feature planning.
- Keep familiar job wording. Submission can say **Job submitted.** A disabled
  action while its job runs can say **A job is already in progress.**
- Use the shared diagnostic catalog and stable message keys for API-provided
  explanations. Queued, waiting, blocked and completed work must be described
  accurately rather than all being called running.
- The future dashboard uses API-provided availability and explanations for
  disabled actions. Styling and hover behavior remain dashboard implementation.

## Internal compatibility and scope

Existing internal names such as `trial`, `storage.trial_root`, job kinds,
module names and SQL identifiers can remain unchanged. Labels can change
without changing the stored configuration key or moving existing media.
Do not rename existing paths, rewrite historical diagnostics or introduce a
configuration/database migration for this terminology decision.

Application branding remains undecided. An arr-style name was discussed but no
final name or repository rename was approved.

## Planning questions still open

Before implementing the viewing/publication portion, settle its folder layout,
what evidence makes an item Ready to watch, and how metadata or Jellyfin refresh
failures are displayed. Keep/Reject scope, rejection persistence and post-decision
retention/cleanup need their own explicit design. This naming decision does not
authorize deletion or silently settle those policies.

The pipeline architecture remains in [intake to the trial folder](INTAKE_TO_USER_EVALUATION.md)
and [Keep to the permanent library](KEEP_TO_LIBRARY.md). Those existing technical
filenames remain stable; this document supersedes their trial/preview wording
for the user-facing Intake and Staging flow.
