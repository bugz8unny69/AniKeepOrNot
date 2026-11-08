"""AniDB-first resolver composed for identify_anime_job; no fuzzy approval."""
from collections import OrderedDict
import re
import threading
from contextlib import closing
from src.anime.storage.recognition import recognized_for_release
from src.anime.storage.migrations import _open, LATEST_VERSION

from src.anime.providers.http import Clients, Credentials, Options, json_object
from src.anime.providers.errors import (ProviderInvalidResponse, ProviderDeferred,
    ProviderUnavailable, ProviderAuthenticationFailed)
from src.anime.release_names import norm, release_title
from src.anime.providers.scoring import generate_title_variants, rank_and_select_candidates
from src.anime.release_names import detect_explicit_sequel_marker
from src.anime.storage.sqlite import IdentityConflict, IdentityNotReady
from src.anime.worker import blocking


def delivery_title(request):
    """Initial explicit JSON input contract; future intake adapters normalize it."""
    if type(request.raw_body) is not bytes or len(request.raw_body)>65536:
        raise IdentityNotReady()
    try:
        document = json_object(request.raw_body)
    except ProviderInvalidResponse:
        raise IdentityNotReady() from None
    value = document.get("release_name")
    if type(value) is not str or not value.strip() or len(value)>2048:
        raise IdentityNotReady()
    if any(ord(char)<32 or ord(char)==127 for char in value): raise IdentityNotReady()
    return value



def _release_seasons(title):
    """Season numbers a release declares: SxxEyy, S2, Season 2, 2nd/Second Season.

    A single episode is read by the shared title reader, so "Season - 05" is
    never taken for season 5. Other titles (packs) keep the original pattern.
    """
    from src.anime.release_names import EXPLICIT, marker_seasons, parse_release
    # Only the primary title may declare the release's actual season. A
    # pipe-delimited alternative may mention another episode (e.g. S01E25).
    primary = title.split(" | ", 1)[0]
    parsed = parse_release(primary)
    if parsed is None:
        return re.findall(r"(?i)(?:\bS|\bSeason[ ._-]*S?)([0-9]{1,2})(?![0-9])", primary)
    found = marker_seasons(EXPLICIT.sub(' ', parsed.text))
    if parsed.season is not None: found.add(parsed.season)
    return [str(number) for number in sorted(found)]


def explicit_sequel_alias(title, base_aliases):
    """Extract a distinct pipe-delimited sequel title as a search hint only."""
    if type(title) is not str or title.count(" | ") != 1 or not base_aliases:
        return None
    suffix = title.split(" | ", 1)[1].strip()
    # Some releases append packaging tokens to the alternate anime name.
    # Strip only known trailing tokens; title commas remain intact.
    suffix = re.sub(
        r"(?i)(?:,\s*(?:dual[- ]audio|multi[- ]subs|multi[- ]audio|batch|complete(?: season)?))+\s*[)\]]*\s*$",
        "", suffix,
    )
    if not suffix or len(suffix.encode("utf-8")) > 256:
        return None
    titles, _ = release_title(suffix)
    if not titles:
        return None
    alias = titles[0]
    if (not 4 <= len(norm(alias)) <= 160
            or len(alias.encode("utf-8")) > 160
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in alias)
            or norm(alias) == norm(base_aliases[0])):
        return None
    return alias


class ClientPool:
    """Share bounded option-specific clients across identity and release jobs."""
    def __init__(self, factory=Clients):
        self.factory,self.clients,self.lock=factory,OrderedDict(),threading.Lock()

    def get(self, database, options):
        if type(options) is not Options: raise ValueError("provider options required")
        with self.lock:
            if any(key[0]!=database for key in self.clients):
                raise ValueError("resolver database changed; rebuild composition")
            key=(database,options)
            client=self.clients.get(key)
            if client is None: client=self.clients[key]=self.factory(database,options)
            self.clients.move_to_end(key)
            while len(self.clients)>4: self.clients.popitem(last=False)
            return client


def make_resolver(options, credentials, *, title_extractor=delivery_title, client_factory=Clients, client_pool=None):
    """Return an async resolver with shared clients/caches for all its attempts.

    Options are trusted service composition, not undocumented settings keys.
    credentials() must return Credentials from the service secret facility;
    neither it nor any API key is read from a webhook or public configuration.
    title_extractor is a trusted normalizer, never a payload-selected callable.
    """
    if (type(options) is not Options and not callable(options)) or not callable(credentials) or not callable(title_extractor):
        raise ValueError("provider composition is incomplete")
    pool = client_pool or ClientPool(client_factory)

    def identify(request, configuration, cancelled, matching=None):
        # One trace per attempted job. Never use trace content to authorize identity.
        stage = "release_title"

        def check(step, status, note=None):
            if matching is not None:
                matching.check(step, status, note)

        def incomplete(note):
            check(stage, "failed", note)
            raise IdentityNotReady()

        try:
            if cancelled.is_set():
                incomplete("canceled")
            actual_options = options(configuration) if callable(options) else options
            if type(actual_options) is not Options:
                raise ValueError("provider options required")
            secret = credentials()
            if type(secret) is not Credentials:
                from src.anime.providers.errors import ProviderAuthenticationFailed
                raise ProviderAuthenticationFailed()
            title = title_extractor(request)
            if type(title) is not str or len(title) > 2048:
                incomplete("invalid_release_title")
            seasons = _release_seasons(title)
            aliases, episode = release_title(title)
            if matching is not None:
                matching.queries(aliases, episode, int(seasons[0]) if len(seasons) == 1 else None)
            if len({int(value) for value in seasons}) > 1:
                stage = "season_scope"
                incomplete("multiple_seasons")
            if not aliases:
                incomplete("no_title_aliases")
            check("release_title", "passed")

            if len(seasons) <= 1:
                from src.anime.storage.manual_identity import for_release
                manual = for_release(configuration.value('storage.database_path'),
                                     title, int(seasons[0]) if seasons else None)
                if manual is not None:
                    if matching is not None:
                        matching.anidb(manual['anime']['anidb_id'])
                        matching.tvdb(manual['anime']['tvdb_series_id'],
                                      (manual['anime']['title'],))
                    return manual
            # First reuse existing season-scoped *verified* observations only.
            database = configuration.value("storage.database_path")
            stage = "recognized_season"
            if seasons:
                with closing(_open(database, readonly=True, timeout_ms=5000)) as known_db:
                    if known_db.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION:
                        known = recognized_for_release(known_db, title, season=int(seasons[0]))
                        if known is not None:
                            if matching is not None:
                                matching.anidb(known["anidb_id"])
                                matching.tvdb(known["tvdb_series_id"], (known["title"],))
                            check(stage, "passed", "recognized_season_reused")
                            check("identity_verified", "passed", "verified")
                            return {"schema_version": 2, "anime": {
                                "anidb_id": known["anidb_id"],
                                "tvdb_series_id": known["tvdb_series_id"],
                                "title": known["title"]}, "episodes": [], "attributes": {}}
                check(stage, "skipped", "not_previously_recognized")
            else:
                check(stage, "skipped")

            # Threshold for candidate eligibility (defaults to 90%)
            try:
                threshold_pct = int(configuration.value("identity.match_threshold_pct"))
            except Exception:
                threshold_pct = 90

            variants, _, _ = generate_title_variants(title, seasons=seasons, episode=episode)

            # S02+ is not itself a rejection. Prefer a separately named
            # sequel work over the generic season-one title for lookup.
            stage = "season_scope"
            season_lookup = None
            base_aliases = None
            alternate_alias = None
            if any(int(value) > 1 for value in seasons):
                sequel = explicit_sequel_alias(title, aliases)
                if sequel is None:
                    # No sequel title: identify the base series, then take the
                    # AniDB entry anime-lists records as its TVDB season N.
                    season_lookup = int(seasons[0])
                    check(stage, "passed", "base_series_season_lookup")
                else:
                    base_aliases, aliases = aliases, [sequel]
                    if matching is not None:
                        matching.queries(aliases, episode, int(seasons[0]))
                    check(stage, "passed", "sequel_alias_selected")
            else:
                # For S01, prefer the primary title but try the distinct pipe
                # alternate when the catalog has no exact primary match.
                alternate_alias = explicit_sequel_alias(title, aliases)
                check(stage, "passed" if seasons else "skipped")

            stage = "anidb_candidates"
            client = pool.get(database, actual_options)
            candidates = client.title_candidates(aliases)
            if not candidates and base_aliases is not None:
                # AniDB does not list the release's sequel title: identify the
                # base series instead and take its anime-lists season entry.
                aliases, season_lookup = base_aliases, int(seasons[0])
                if matching is not None:
                    matching.queries([*matching.data["queries"], *aliases], episode, season_lookup)
                check("season_scope", "passed", "base_series_season_lookup")
                candidates = client.title_candidates(aliases)
            elif not candidates and alternate_alias is not None:
                aliases = [alternate_alias]
                if matching is not None:
                    matching.queries([*matching.data["queries"], *aliases],
                                     episode, int(seasons[0]) if seasons else None)
                candidates = client.title_candidates(aliases)

            if not candidates:
                # Search using any remaining title variants
                other_variants = [v.text for v in variants if v.text not in aliases]
                if other_variants:
                    extra = client.title_candidates(other_variants)
                    if extra:
                        candidates.update(extra)
            elif isinstance(client, Clients):
                # When searching real offline catalog, discover candidates across all variants
                extra_variants = [v.text for v in variants if v.text not in aliases]
                if extra_variants:
                    extra = client.title_candidates(extra_variants)
                    for k, v in extra.items():
                        if k not in candidates:
                            candidates[k] = v
                        else:
                            candidates[k] = tuple(dict.fromkeys((*candidates[k], *v)))

            if matching is not None:
                matching.candidates(candidates)
            if len(candidates) == 0:
                if matching is not None:
                    matching.scoring(threshold_pct, "v1", "NO_CANDIDATES", 0, 0)
                incomplete("no_anidb_candidates")

            # Score and rank all discovered AniDB candidates
            rel_season = int(seasons[0]) if seasons else None
            scoring = rank_and_select_candidates(
                candidates, variants, threshold_pct,
                release_season=rel_season
            )

            if matching is not None:
                matching.set_candidate_evidence(scoring.candidates)
                eligible_cnt = sum(1 for c in scoring.candidates if c.eligible)
                matching.scoring(
                    threshold_pct, scoring.scoring_version, scoring.decision,
                    len(scoring.candidates), eligible_cnt
                )

            if scoring.selected is None:
                if scoring.decision == "AMBIGUOUS_TIE":
                    incomplete("no_unique_anidb_candidate")
                else:
                    incomplete("no_anidb_candidates")

            check(stage, "passed")
            aid = scoring.selected.anidb_id
            if matching is not None:
                matching.anidb(aid)

            if cancelled.is_set():
                incomplete("canceled")
            stage = "anidb_record"
            anime = client.anime(aid)
            if not {norm(x) for x in anime["titles"]} & {norm(x) for x in candidates[aid]}:
                check(stage, "failed", "anidb_titles_disagree")
                raise IdentityConflict()
            check(stage, "passed")

            stage = "anime_lists"
            tid = client.mapping(aid)
            if matching is not None:
                matching.tvdb(tid, ())
            base_aid, base_anime = aid, anime

            # If the selected AniDB work already corresponds to the release's season,
            # no further redirection via Anime-Lists season_entry is needed.
            cand_base, cand_season = detect_explicit_sequel_marker(anime["title"])
            if cand_season is None and scoring.selected is not None:
                cand_base, cand_season = detect_explicit_sequel_marker(scoring.selected.matched_anidb_title)
            if rel_season is not None and cand_season == rel_season:
                season_lookup = None

            if season_lookup is not None:
                aid = client.season_entry(tid, season_lookup, episode)
                if matching is not None:
                    matching.anidb(aid)
                anime = client.anime(aid)
                check(stage, "passed", "season_entry_selected")
            else:
                check(stage, "passed")
            if cancelled.is_set():
                incomplete("canceled")

            stage = "tvdb_series"
            tvdb = client.series(tid, secret)
            if matching is not None:
                matching.tvdb(tid, tvdb["titles"])
            check(stage, "passed")
            if tvdb["live"]:
                check("tvdb_animation", "failed", "tvdb_live_action")
                raise IdentityConflict()
            if not tvdb["animation"]:
                stage = "tvdb_animation"
                incomplete("tvdb_not_animation")
            check("tvdb_animation", "passed")

            stage = "provider_link"
            # TVDB links the series to its first AniDB entry; anime-lists links the season.
            if tvdb["anidb_ids"] and base_aid not in tvdb["anidb_ids"] and aid not in tvdb["anidb_ids"]:
                check(stage, "failed", "tvdb_anidb_conflict")
                raise IdentityConflict()
            check(stage, "passed")
            stage = "provider_titles"
            has_id_link = bool(tvdb["anidb_ids"] and (base_aid in tvdb["anidb_ids"] or aid in tvdb["anidb_ids"]))
            titles_agree = bool({norm(x) for x in base_anime["titles"]} & {norm(x) for x in tvdb["titles"]})
            if not has_id_link and not titles_agree:
                check(stage, "failed", "provider_titles_disagree")
                raise IdentityConflict()
            check(stage, "passed")
            if cancelled.is_set():
                incomplete("canceled")
            check("identity_verified", "passed", "verified")
            result_anime = {"anidb_id": aid, "tvdb_series_id": tid, "title": anime["title"]}
            for name in ("english_title", "original_title"):
                if anime.get(name):
                    result_anime[name] = anime[name]
            return {"schema_version": 2, "anime": result_anime,
                    "episodes": [], "attributes": (
                        {"romaji_title": anime["romaji_title"]}
                        if anime.get("romaji_title") else {})}
        except ProviderDeferred:
            check(stage, "waiting", "upstream_deferred")
            raise
        except ProviderUnavailable:
            check(stage, "waiting", "upstream_unavailable")
            raise
        except ProviderInvalidResponse:
            check(stage, "failed", "invalid_provider_response")
            raise
        except IdentityNotReady:
            if matching is not None and not any(
                    check["step"] == stage and check["status"] == "failed"
                    for check in matching.data["checks"]):
                check(stage, "failed", "missing_verification")
            raise
        except IdentityConflict:
            if matching is not None and not any(
                    check["step"] == stage and check["status"] == "failed"
                    for check in matching.data["checks"]):
                check(stage, "failed", "identity_conflict")
            raise

    async def resolve(request, configuration, cancelled, *, matching=None):
        return await blocking(identify, request, configuration, cancelled, matching)

    return resolve
