# Manual identification recovery

When an IDENTIFY_ANIME job is in NEEDS_REVIEW, open **Matching details** and enter the title, AniDB work ID, TVDB series ID and season. Saving records an operator-confirmed mapping from the normalized release title and that **TVDB season** to the chosen IDs. The existing Retry action then queues the **same job**, without creating a new identification job.

Later retries for releases with the same normalized title and season can reuse the entered series identity. A different season does not inherit the mapping, because AniDB may use a separate work per season. Per-episode mapping remains the responsibility of existing release verification; this feature never inserts records into recognized_episodes and never manufactures episode evidence.

Storage: schema v49 adds manual_identity_matches. The API endpoint POST /api/v1/jobs/{id}/manual-match requires a signed-in session, CSRF protection, and a reviewed identification job. Conflicting edits are rejected rather than silently replacing existing matches.

This feature does not query AniDB or TVDB when accepting a manual match: the operator is explicitly supplying the series identity. It does not provide a title lookup UI. Failed retries remain reviewable through the existing job history. Local tests: python -m unittest src.anime.tests.test_manual_identity src.anime.tests.test_matching_details (not executed in this environment). No CI run requested.

## Specials and OVAs

The existing `batch.recognition.release_scope_hint()` classifies regular episodes, season packs, S00 specials, unnumbered OVAs/ONAs, and ambiguous releases. The manual identity lookup uses that classification rather than defining a second parser. An explicit S00 uses season 0; unnumbered special/OVA/ONA uses a distinct internal scope (-1). Ambiguous releases are not automatically reused. The form does not require an episode or season number. Episode mapping is always verified separately.
