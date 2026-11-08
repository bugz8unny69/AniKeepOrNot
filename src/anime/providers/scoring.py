"""AniDB candidate scoring, title similarity, and explainable ranking.

Architecture rules:
- AniDB remains the authoritative source for anime identity.
- 90% (or identity.match_threshold_pct) is an eligibility floor, not a selection.
- All discovered candidates are evaluated before selecting a winner.
- Multiple candidates above threshold do not cause NEEDS_REVIEW.
- Ranking is explainable, evidence-based, and deterministic.
"""
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from src.anime.release_names import FOUR_DIGIT_YEAR, SPECIAL_WORDS, YEAR_TAG, clean_metadata, detect_explicit_sequel_marker, norm


@dataclass(frozen=True)
class TitleVariant:
    """A search variant derived from a release title, preserving provenance."""
    text: str
    provenance: str  # "original", "cleaned", "explicit_alternate", "explicit_sequel", "season_derived"

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("title variant text must be non-empty string")
        if self.provenance not in (
            "original", "cleaned", "explicit_alternate", "explicit_sequel", "season_derived"
        ):
            raise ValueError(f"invalid provenance: {self.provenance}")


@dataclass(frozen=True)
class CandidateMatch:
    """An AniDB candidate evaluated against release title variants."""
    anidb_id: int
    display_title: str
    all_titles: tuple[str, ...]
    matched_anidb_title: str
    matched_variant: TitleVariant
    similarity_pct: int
    rank: int = 0
    ranking_score: float = 0.0
    eligible: bool = False
    selected: bool = False
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScoringResult:
    """Outcome of scoring and ranking all AniDB candidates."""
    candidates: tuple[CandidateMatch, ...]
    selected: CandidateMatch | None
    decision: str  # "AUTO_SELECTED", "NO_ELIGIBLE_CANDIDATES", "AMBIGUOUS_TIE"
    decision_reason: str
    threshold_pct: int
    scoring_version: str = "v1"


def generate_season_derived_aliases(base_title, season):
    """Generate meaningful season-aware title variants when season > 1."""
    if not base_title or not isinstance(season, int) or season <= 1:
        return []
    ordinals = {2: '2nd', 3: '3rd', 4: '4th', 5: '5th', 6: '6th'}
    ordinal_words = {2: 'Second', 3: 'Third', 4: 'Fourth', 5: 'Fifth', 6: 'Sixth'}
    roman_numerals = {2: 'II', 3: 'III', 4: 'IV', 5: 'V', 6: 'VI'}

    derived = []
    if season in ordinals:
        derived.append(f"{base_title} {ordinals[season]} Season")
    if season in ordinal_words:
        derived.append(f"{base_title} {ordinal_words[season]} Season")
    derived.append(f"{base_title} Season {season}")
    derived.append(f"{base_title} S{season}")
    if season in roman_numerals:
        derived.append(f"{base_title} {roman_numerals[season]}")
    return derived


def generate_title_variants(raw, *, seasons=None, episode=None):
    """Parse all legitimate title variants from a release string, preserving provenance.

    Returns:
        (tuple_of_TitleVariant, detected_season, detected_episode)
    """
    if not isinstance(raw, str) or not raw.strip():
        return (), None, None

    # Step 1: Remove leading group bracket tags e.g. [SubsPlease], [Anime Time]
    leading_groups = re.compile(r'^\s*(?:[\[【][^\]】]*[\]】]\s*)+')
    body = leading_groups.sub('', raw.strip()).strip()

    # Step 2: Detect episode and season markers
    from src.anime.release_names import parse_release, EXPLICIT, DASHED, WORDED, TRAILING
    parsed = parse_release(raw)

    detected_season = None
    detected_episode = None
    if parsed is not None:
        detected_season = parsed.season
        detected_episode = parsed.episode
    else:
        # Fallback regex detection for single episode or movie
        ep_match = EXPLICIT.search(body)
        if ep_match:
            detected_season = int(ep_match.group(1))
            detected_episode = int(ep_match.group(2))
        else:
            dash_match = DASHED.search(body) or WORDED.search(body) or TRAILING.search(body)
            if dash_match:
                detected_episode = int(dash_match.group(1))

    # Incorporate explicitly passed seasons if any
    effective_season = detected_season
    if effective_season is None and seasons:
        try:
            effective_season = int(seasons[0])
        except (ValueError, TypeError, IndexError):
            pass

    # Step 3: Split pipe-delimited alternatives
    # E.g. "[Dyna] Higurashi When They Cry S02 ... | Higurashi Sotsu"
    parts = [p.strip() for p in re.split(r'\s+[|]\s+', body) if p.strip()]
    if not parts:
        parts = [body]

    variants = []
    seen_texts = set()

    def add_variant(text, provenance):
        # Basic sanitization
        t = text.strip()
        while t.count('(') > t.count(')'):
            m = re.search(r'\([^()]*$', t)
            if not m: break
            t = t[:m.start()].strip()
        while t.count(')') > t.count('('):
            t = re.sub(r'\)[^()]*$', '', t).strip()
        t = re.sub(r'[\(\[\{:\-\s]+$', '', t).strip()
        t = re.sub(r'\s+', ' ', t).strip(' ._-:,')
        if not t or re.fullmatch(r'^[A-Za-z]?\d{6,}$', t) or re.fullmatch(r'^[0-9a-fA-F]{16,}$', t):
            return
        n = norm(t)
        if not n or n in seen_texts:
            return
        seen_texts.add(n)
        variants.append(TitleVariant(t, provenance))

    # Process each pipe-delimited part
    for idx, part in enumerate(parts):
        is_primary = (idx == 0)
        default_prov = "cleaned" if is_primary else "explicit_alternate"

        # Check if this part contains explicit slash-delimited titles (e.g. Romaji / English)
        slash_subparts = [sp.strip() for sp in re.split(r'\s+/\s+', part) if sp.strip()]
        for subpart in slash_subparts:
            # Strip episode/season tail if present in subpart
            match = EXPLICIT.search(subpart) or DASHED.search(subpart) or WORDED.search(subpart) or TRAILING.search(subpart)
            sub_title = subpart[:match.start()].strip() if match else subpart

            # Clean tech and version metadata (e.g. v4, 1080p)
            cleaned_sub = clean_metadata(sub_title)
            if not cleaned_sub:
                continue

            # Check if this cleaned title has an explicit sequel marker (e.g. "2nd Season")
            base, seq_num = detect_explicit_sequel_marker(cleaned_sub)
            if seq_num is not None and base != cleaned_sub:
                # Both the explicit sequel title and the base title are valuable!
                add_variant(cleaned_sub, "explicit_sequel")
                add_variant(base, default_prov)
                if effective_season is None:
                    effective_season = seq_num
            else:
                add_variant(cleaned_sub, default_prov)

            # Year-stripped variant if year parenthetical is present
            if YEAR_TAG.search(cleaned_sub):
                unyeared = re.sub(r'\s+', ' ', YEAR_TAG.sub(' ', cleaned_sub)).strip(' -:,')
                if unyeared:
                    add_variant(unyeared, default_prov)

    # Step 4: For base titles, generate season-aware derived variants if season > 1
    if effective_season is not None and effective_season > 1:
        # Find base variants (cleaned or explicit_alternate) that do not already have sequel markers
        bases_to_derive = []
        for v in list(variants):
            if v.provenance in ("cleaned", "original", "explicit_alternate"):
                base, num = detect_explicit_sequel_marker(v.text)
                if num is None and base:
                    bases_to_derive.append(base)

        for base in bases_to_derive:
            for derived_text in generate_season_derived_aliases(base, effective_season):
                add_variant(derived_text, "season_derived")

    return tuple(variants), effective_season, detected_episode


def calculate_title_similarity(variant_text, candidate_title):
    """Highest normalized character similarity between a variant and candidate title."""
    norm_v = norm(variant_text)
    norm_c = norm(candidate_title)
    if not norm_v or not norm_c:
        return 0
    ratio = SequenceMatcher(None, norm_v, norm_c, autojunk=False).ratio()
    return round(100 * ratio)


def score_anidb_candidate(
    candidate_aid, candidate_titles, variants, threshold_pct,
    *, release_season=None, release_year=None
):
    """Evaluate an AniDB candidate against release title variants.

    Computes:
    - Best title similarity percentage (determines eligibility against threshold floor).
    - Explainable composite ranking score for eligible candidates.
    """
    if not candidate_titles or not variants:
        return CandidateMatch(
            anidb_id=candidate_aid,
            display_title=candidate_titles[0] if candidate_titles else "",
            all_titles=candidate_titles or (),
            matched_anidb_title="",
            matched_variant=TitleVariant("unknown", "cleaned"),
            similarity_pct=0,
            eligible=False,
            evidence={"reason": "NO_USABLE_TITLES"}
        )

    display_title = candidate_titles[0]
    best_sim = -1
    best_pair = None

    for variant in variants:
        norm_v = norm(variant.text)
        for c_title in candidate_titles:
            norm_c = norm(c_title)
            sim = calculate_title_similarity(variant.text, c_title)
            # Track best match (prefer exact match or higher similarity)
            if sim > best_sim:
                best_sim = sim
                best_pair = (variant, c_title)
            elif sim == best_sim and best_pair is not None:
                # Tie break on exact normalized match or stronger provenance
                prov_order = {
                    "explicit_sequel": 5, "explicit_alternate": 4,
                    "cleaned": 3, "original": 2, "season_derived": 1
                }
                cur_prov = prov_order.get(variant.provenance, 0)
                best_prov = prov_order.get(best_pair[0].provenance, 0)
                if (norm_v == norm_c and norm(best_pair[0].text) != norm(best_pair[1])) or (cur_prov > best_prov):
                    best_sim = sim
                    best_pair = (variant, c_title)

    matched_variant, matched_title = best_pair
    is_eligible = (best_sim >= threshold_pct)

    # Ranking score calculation
    ranking_score = float(best_sim)
    evidence = {
        "similarity_pct": best_sim,
        "threshold_pct": threshold_pct,
        "matched_variant": matched_variant.text,
        "matched_variant_provenance": matched_variant.provenance,
        "matched_anidb_title": matched_title,
    }

    # 1. Exact match bonus
    is_exact = (norm(matched_variant.text) == norm(matched_title))
    if is_exact:
        ranking_score += 10.0
        evidence["exact_match_bonus"] = 10.0

    # 2. Provenance weight
    prov_weights = {
        "explicit_sequel": 8.0,
        "explicit_alternate": 6.0,
        "cleaned": 5.0,
        "original": 4.0,
        "season_derived": 3.0,
    }
    prov_bonus = prov_weights.get(matched_variant.provenance, 0.0)
    ranking_score += prov_bonus
    evidence["provenance_weight"] = prov_bonus

    # 3. Season agreement
    cand_base, cand_season = detect_explicit_sequel_marker(matched_title)
    if cand_season is None:
        cand_base, cand_season = detect_explicit_sequel_marker(display_title)

    if release_season is not None:
        if cand_season is not None:
            if cand_season == release_season:
                ranking_score += 10.0
                evidence["season_agreement"] = 10.0
            else:
                ranking_score -= 20.0
                evidence["season_contradiction"] = -20.0
        else:
            # Candidate has no season marker while release is Season 2+
            if release_season > 1:
                evidence["season_neutral"] = 0.0

    # 4. Spinoff / Special / Non-Regular Penalty
    is_special_candidate = bool(
        SPECIAL_WORDS.search(matched_title) or SPECIAL_WORDS.search(display_title)
    )
    if is_special_candidate and (release_season is None or release_season > 0):
        # Unless the release itself explicitly mentions special, penalize spinoff/special candidates
        ranking_score -= 15.0
        evidence["special_penalty"] = -15.0

    # 5. Year agreement
    if release_year is not None:
        cand_year_match = FOUR_DIGIT_YEAR.search(matched_title) or FOUR_DIGIT_YEAR.search(display_title)
        if cand_year_match:
            try:
                cy = int(cand_year_match.group(0))
                if cy == release_year:
                    ranking_score += 5.0
                    evidence["year_agreement"] = 5.0
                else:
                    ranking_score -= 10.0
                    evidence["year_contradiction"] = -10.0
            except ValueError:
                pass

    # 6. AniDB title specificity: Main / Official title bonus
    if matched_title == display_title:
        ranking_score += 3.0
        evidence["main_title_bonus"] = 3.0

    evidence["final_ranking_score"] = round(ranking_score, 2)

    return CandidateMatch(
        anidb_id=candidate_aid,
        display_title=display_title,
        all_titles=candidate_titles,
        matched_anidb_title=matched_title,
        matched_variant=matched_variant,
        similarity_pct=best_sim,
        ranking_score=ranking_score,
        eligible=is_eligible,
        evidence=evidence
    )


def rank_and_select_candidates(
    candidates_dict, variants, threshold_pct,
    *, release_season=None, release_year=None
):
    """Score all candidates, filter by threshold floor, rank eligible, and select winner.

    Returns:
        ScoringResult
    """
    if not candidates_dict or not variants:
        return ScoringResult(
            candidates=(),
            selected=None,
            decision="NO_CANDIDATES",
            decision_reason="No AniDB candidates discovered",
            threshold_pct=threshold_pct
        )

    # 1. Score all candidates
    scored = []
    for aid, titles in candidates_dict.items():
        match = score_anidb_candidate(
            aid, titles, variants, threshold_pct,
            release_season=release_season,
            release_year=release_year
        )
        scored.append(match)

    # 2. Sort all candidates deterministically:
    # First by eligibility (eligible first)
    # Then by ranking_score DESC
    # Then by similarity_pct DESC
    # Then by anidb_id ASC (deterministic tie breaker)
    scored.sort(
        key=lambda c: (
            1 if c.eligible else 0,
            c.ranking_score,
            c.similarity_pct,
            -c.anidb_id
        ),
        reverse=True
    )

    # Assign ranks (1-based)
    ranked = []
    for idx, cand in enumerate(scored, 1):
        ranked.append(CandidateMatch(
            anidb_id=cand.anidb_id,
            display_title=cand.display_title,
            all_titles=cand.all_titles,
            matched_anidb_title=cand.matched_anidb_title,
            matched_variant=cand.matched_variant,
            similarity_pct=cand.similarity_pct,
            rank=idx,
            ranking_score=cand.ranking_score,
            eligible=cand.eligible,
            selected=False,
            evidence=cand.evidence
        ))

    eligible_candidates = [c for c in ranked if c.eligible]

    if not eligible_candidates:
        return ScoringResult(
            candidates=tuple(ranked),
            selected=None,
            decision="NO_ELIGIBLE_CANDIDATES",
            decision_reason=f"All candidates scored below the configured similarity threshold of {threshold_pct}%",
            threshold_pct=threshold_pct
        )

    # 3. Check for genuine ambiguous ties at rank 1:
    # Two candidates are tied IF they have the EXACT SAME ranking score AND similarity % AND identical titles/season evidence
    if len(eligible_candidates) > 1:
        c1, c2 = eligible_candidates[0], eligible_candidates[1]
        if (c1.ranking_score == c2.ranking_score and
                c1.similarity_pct == c2.similarity_pct and
                norm(c1.matched_anidb_title) == norm(c2.matched_anidb_title) and
                c1.all_titles == c2.all_titles):
            # True ambiguous tie between identical works
            return ScoringResult(
                candidates=tuple(ranked),
                selected=None,
                decision="AMBIGUOUS_TIE",
                decision_reason=f"Multiple AniDB candidates ({c1.anidb_id}, {c2.anidb_id}) have identical scores and titles without distinguishing evidence",
                threshold_pct=threshold_pct
            )

    # 4. Select the highest-ranked eligible candidate
    winner = eligible_candidates[0]
    final_candidates = []
    for c in ranked:
        if c.anidb_id == winner.anidb_id:
            c = CandidateMatch(
                anidb_id=c.anidb_id,
                display_title=c.display_title,
                all_titles=c.all_titles,
                matched_anidb_title=c.matched_anidb_title,
                matched_variant=c.matched_variant,
                similarity_pct=c.similarity_pct,
                rank=c.rank,
                ranking_score=c.ranking_score,
                eligible=c.eligible,
                selected=True,
                evidence={**c.evidence, "selection_reason": "Highest ranked eligible AniDB candidate"}
            )
            selected_match = c
        final_candidates.append(c)

    return ScoringResult(
        candidates=tuple(final_candidates),
        selected=selected_match,
        decision="AUTO_SELECTED",
        decision_reason=f"Selected AniDB #{winner.anidb_id} ({winner.display_title}) with {winner.similarity_pct}% similarity (floor: {threshold_pct}%)",
        threshold_pct=threshold_pct
    )
