# Anime Discovery NextGen — AniDB Identity Matching & Candidate Ranking

**Status:** Implemented (Schema v59, Migration `059_identity_candidate_evidence.sql`)  
**Target:** NextGen  
**Configuration:** `identity.match_threshold_pct` (Default: `90`, Range: `50–100`)

---

## 1. Overview & Architectural Principles

The NextGen `IDENTIFY_ANIME` job stage previously enforced strict exact-string AniDB title lookups and automatically failed with `IDENTITY_INCOMPLETE / NEEDS_REVIEW` whenever zero or multiple AniDB candidates matched. This caused avoidable identification failures on releases with minor metadata noise (`v4`), multi-title releases (English/Japanese/Romaji), and sequels (`2nd Season`).

The improved pipeline introduces comprehensive candidate discovery, multi-variant extraction, configurable similarity-floor scoring, evidence-based candidate ranking, and relational diagnostic persistence:

$$\text{Release Title} \longrightarrow \text{Variant Extraction} \longrightarrow \text{AniDB Discovery} \longrightarrow \text{Candidate Scoring} \longrightarrow \text{90\% Floor} \longrightarrow \text{Best Candidate Winner} \longrightarrow \text{Anime-Lists TVDB Crossref}$$

### Architectural Boundaries & Invariants

1. **AniDB Remains Authoritative:** AniDB is the sole authoritative catalog for anime identity.
2. **Anime-Lists Downstream Only:** Anime-Lists never selects, ranks, or disambiguates AniDB entries. It is consulted strictly downstream to map the already-selected AniDB identity to TVDB.
3. **Threshold Semantics:** The configured threshold (`identity.match_threshold_pct`, default 90%) establishes candidate **eligibility**, not the winner. Multiple candidates above 90% do not cause `NEEDS_REVIEW`; all candidates are evaluated and the highest-ranked eligible candidate wins.
4. **Offline Isolation:** Candidate discovery and ranking operate strictly against the cached local AniDB catalog (`anidb_titles`). No network or HTTP requests occur per candidate during discovery or ranking.
5. **Downstream Safeguards Unaltered:** Title matching confidence does not bypass or relax downstream episode mapping, verification gates, or acquisition safety checks.
6. **Operator Overrides Sovereign:** Manual identity overrides and previously verified Recognized Names retain strict precedence over automatic matching.

---

## 2. Release Title Parsing & Multi-Variant Discovery

The parser in `src/anime/providers/scoring.py` and `src/anime/providers/parsing.py` extracts all legitimate title variants from a release string while preserving provenance.

### Title Variant Provenance

Each derived variant is tagged with one of five provenance categories:

| Provenance | Description | Example |
|---|---|---|
| `original` | Raw release title without enclosing group tags | `Black Clover - S02E02` |
| `cleaned` | Version tokens (`v4`), tech specs, codecs, and container tags stripped | `Majestic Prince: Genetic Awakening (2016)` |
| `explicit_alternate` | Pipe-delimited (`\|`) or slash-delimited (`/`) alternate names (English/Japanese/Romaji) | `Higurashi Sotsu` from `... When They Cry S02 \| Higurashi Sotsu` |
| `explicit_sequel` | Titles where a sequel marker was explicitly present in the written title | `Black Clover 2nd Season` |
| `season_derived` | Season-aware aliases generated for base titles when release season > 1 | `Black Clover Season 2`, `Black Clover 2nd Season` |

### Explicit Sequel vs. Speculative Season Derivation

A critical distinction is maintained:
- An explicit sequel title written directly in the release (e.g. `[Group] Show 2nd Season - 01`) receives high provenance weight (`explicit_sequel`, +8.0 ranking bonus).
- A speculative alias derived from episode notation (e.g. `[Group] Show - S02E01` generating `Show 2nd Season`) receives lower provenance weight (`season_derived`, +3.0 ranking bonus) and cannot override contradictory evidence.

### Release Metadata Sanitization

`clean_metadata()` strips non-title noise without mutilating title-embedded years:
- **Version tokens:** `v1`, `v2`, `v3`, `v4`
- **Resolution & Source:** `1080p`, `720p`, `4K`, `BluRay`, `BDRip`, `WEBRip`, `WEB-DL`, `Remux`
- **Video & Audio Codecs:** `HEVC`, `x264`, `x265`, `AV1`, `10-bit`, `FLAC`, `AAC 2.0`, `Opus`
- **Packaging & Checksums:** `Batch`, `Complete Season`, `[0123ABCD]`, `Dual Audio`
- **Year Preservation:** Release years embedded in titles (e.g. `Steamboy (2004)`) are preserved so candidate year matching can verify them against AniDB.

---

## 3. Scoring & Best-Candidate Ranking Model

Candidate evaluation is implemented in `src/anime/providers/scoring.py`.

### 1. Title Similarity (Eligibility Floor)

For each candidate, every title variant is compared against all AniDB titles for that entry using normalized character sequence matching:

$$\text{Similarity \%} = \text{round}\left(100 \times \text{SequenceMatcher}(\text{norm}(v), \text{norm}(c)).\text{ratio}()\right)$$

where $\text{norm}()$ applies Unicode NFKC normalization, lowercase conversion, and punctuation normalization.

- **Floor Check:** A candidate is eligible for automatic selection if and only if $\text{Similarity \%} \ge \text{identity.match_threshold_pct}$.
- Candidates below the floor are retained in diagnostics as near-miss evidence but are ineligible for automatic selection.

### 2. Composite Ranking Score

All candidates (eligible and ineligible) receive an explainable ranking score starting from the best title similarity percentage:

$$\text{Ranking Score} = \text{Similarity \%} + \text{Bonuses} - \text{Penalties}$$

| Component | Condition | Value |
|---|---|---|
| **Base Similarity** | Normalized character similarity | 0.0 to 100.0 |
| **Exact Match Bonus** | `norm(variant) == norm(candidate_title)` | **+10.0** |
| **Provenance Bonus** | `explicit_sequel` | **+8.0** |
| | `explicit_alternate` | **+6.0** |
| | `cleaned` | **+5.0** |
| | `original` | **+4.0** |
| | `season_derived` | **+3.0** |
| **Season Agreement** | Candidate title contains explicit season matching release season | **+10.0** |
| **Season Contradiction** | Candidate title specifies a different season than release | **-20.0** |
| **Spinoff/Special Penalty** | Candidate is OVA/Special/Movie while release is regular TV episode | **-15.0** |
| **Year Agreement** | 4-digit release year matches candidate title year | **+5.0** |
| **Year Contradiction** | 4-digit release year contradicts candidate title year | **-10.0** |
| **Main Title Bonus** | Matched title is AniDB primary/main display title | **+3.0** |

### 3. Winner Selection & Tie-Breaking

1. Candidates are sorted deterministically:
   - Primary: `eligible` (True before False)
   - Secondary: `ranking_score` (Descending)
   - Tertiary: `similarity_pct` (Descending)
   - Quaternary: `anidb_id` (Ascending, deterministic fallback)
2. **Ambiguous Tie Check:** If two or more top candidates share identical ranking scores, identical similarity percentages, and identical normalized titles without distinguishing evidence, the matcher halts selection and sets decision to `AMBIGUOUS_TIE`, raising `IdentityNotReady("no_unique_anidb_candidate")` for safe operator triage.
3. Otherwise, the highest-ranked eligible candidate is selected (`AUTO_SELECTED`).

---

## 4. Relational Persistence & Diagnostics (Schema v59)

Candidate matching evidence is permanently persisted in SQLite via migration `059_identity_candidate_evidence.sql`.

### Table Schema

```sql
CREATE TABLE IF NOT EXISTS identity_candidate_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    anidb_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    similarity_pct INTEGER NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    matched_variant TEXT NOT NULL,
    matched_variant_provenance TEXT NOT NULL,
    matched_anidb_title TEXT NOT NULL,
    display_title TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_identity_candidate_evidence_job_attempt
ON identity_candidate_evidence(job_id, attempt_number);
```

### CLI Reporting

Inspect candidate rankings and decision rationales using:

```bash
adm report job --id 449 --include-matching identity
```

Example output:
```text
IDENTIFY_ANIME — Candidate Matching

Release: [Anime Time] Black Clover - S02E02
Threshold: 90%
Scoring version: v1

Parsed:
  Title: Black Clover
  Season: 2
  Episode: 2

Candidates:
  AniDB 19433 — Black Clover 2nd Season
    Similarity: 100%
    Evidence: exact_match_bonus=+10.0, provenance_weight=+3.0 (season_derived), season_agreement=+10.0
    Rank: 1
    Selected: YES

  AniDB 12665 — Black Clover (2017)
    Similarity: 100%
    Evidence: exact_match_bonus=+10.0, provenance_weight=+5.0 (cleaned), main_title_bonus=+3.0
    Rank: 2
    Selected: NO

  AniDB 12351 — Black Clover Special
    Similarity: 100%
    Evidence: exact_match_bonus=+10.0, provenance_weight=+5.0 (cleaned), special_penalty=-15.0
    Rank: 3
    Selected: NO

Decision:
  AUTO_SELECTED (Selected AniDB #19433 with 100% similarity)
```

JSON output (`adm report job --id 449 --format json`) includes the complete candidate evidence array under `doc["matching"]["identity"]["candidate_evidence"]`.

### Web Dashboard

In the Web UI under Jobs, clicking **Matching details** on an `IDENTIFY_ANIME` job renders structured ranking cards showing:
- Candidate rank and AniDB ID
- Matched title and matched release variant
- Similarity percentage and eligibility badge
- Detailed score breakdown (bonuses and penalties)
- Clear indicator of the selected winner

---

## 5. Operator Correction & Precedence Hierarchy

Operator sovereignty is preserved through a strict multi-layer resolution order:

1. **Manual Identity Matches (`manual_identity_matches`):**
   - If an operator has assigned an explicit mapping for a release title and season via the Dashboard or API (`POST /api/v1/jobs/{id}/manual-match`), this mapping is returned immediately.
   - Automatic candidate discovery and scoring are bypassed.
   - Manual mappings are never overwritten by automatic matching.
2. **Recognized Names & Seasons (`recognized_seasons`):**
   - If an existing verified observation exists for the normalized release title and season, it is reused.
3. **Automatic Candidate Discovery & Scoring:**
   - Evaluated only when neither a manual match nor an existing recognized season exists.

### Correcting an Identity via API / Dashboard

To override an identification:
1. Open **Matching details** in the dashboard or execute:
   ```bash
   curl -X POST http://localhost:8080/api/v1/jobs/<JOB_ID>/manual-match \
     -H "Content-Type: application/json" \
     -d '{"anidb_id": 19433, "tvdb_series_id": 331753, "title": "Black Clover", "season": 2}'
   ```
2. Trigger retry on the existing job. The job will reuse the manual match and proceed to downstream episode verification.

---

## 6. Historical Regression Evaluation (32 Cases)

The offline evaluation tool `src/anime/tools/evaluate_historical_identities.py` compares the baseline exact-string matcher against the improved candidate ranking model across 32 historical `IDENTIFY_ANIME` failure cases.

### Summary Metrics

| Metric | Baseline Matcher | Proposed Matcher |
|---|---|---|
| Total Historical Cases | 32 | 32 |
| Successfully Identified | 18 (56.2%) | **23 (71.9%)** |
| Recovered Historical Failures | — | **6 (42.9% recovery)** |
| Legitimately Retained in Review | 14 | **9** |
| False Approvals | 0 | **0** |

### Recovered Failures

1. **#449 (`[Anime Time] Black Clover - S02E02`):**
   - *Baseline:* Failed with `MULTIPLE_ANIDB_CANDIDATES` between AniDB 12351 and 12665; missed AniDB 19433 entirely.
   - *Proposed:* Discovers 19433, 12665, and 12351. Ranks 19433 at Rank 1 (score 123.0) via season agreement (+10.0) and selects it.
2. **#258 (`Majestic Prince: Genetic Awakening (2016) v4`):**
   - *Baseline:* Failed with `NO_ANIDB_CANDIDATES` due to release metadata noise (`v4`).
   - *Proposed:* Cleans metadata, discovers AniDB 12033 at 100% similarity, and automatically selects it.
3. **#441 (`[Dyna] Higurashi When They Cry S02 ... | Higurashi Sotsu`):**
   - *Baseline:* Failed with `NO_ANIDB_CANDIDATES` because alternate title was discarded.
   - *Proposed:* Extracts alternate title variant `Higurashi Sotsu`, matches AniDB 16137 at 100%, and selects it.
4. **#104 (`[Judas] Kimetsu no Yaiba: Mugen Ressha-hen`):** Discovers and selects AniDB 15151 (100% similarity).
5. **#105 (`[Coalgirls] Steins;Gate: Ouka Kouka no Poriomania`):** Discovers and selects AniDB 8774 (100% similarity).
6. **#107 (`Oshi no Ko (2023) [WEBRip 1080p]`):** Discovers and selects AniDB 17214 (100% similarity).

### Legitimately Retained in Review (9 Cases)

The matcher correctly prevents false approvals for cases that require operator intervention or downstream handling:
- **#577:** AniDB identity correctly identified (99991), but Anime-Lists lacks TVDB cross-reference (`IdentityNotReady("no_tvdb_mapping")`). Title matcher correctly preserves downstream failure.
- **#6200:** Season pack release (`multiple_seasons`) rejected at parser boundary.
- **#154–#158:** Malformed, numeric-only, or empty titles (`no_title_aliases`, `invalid_release_title`) safely halted.
- **#120:** Completely nonexistent show scoring below 90% floor (`no_anidb_candidates`).
- **#121:** Ambiguous identical title tie without distinguishing evidence (`AMBIGUOUS_TIE`).
