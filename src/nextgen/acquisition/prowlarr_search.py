"""Read-only Prowlarr release search for backfill and continue.

Prowlarr only forwards the query to its indexers and returns raw results. This
module filters them by the verified anime's titles and episode number, reads
resolution from the title and ranks with DiscoveryPolicy. It never registers,
grabs or downloads anything; qBittorrent acquisition happens downstream.
"""
from dataclasses import dataclass, field
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from src.nextgen.acquisition.qbittorrent import _NoRedirect, _json
from src.nextgen.acquisition.audio import dual_audio_tagged, wants_english
from src.nextgen.acquisition.settings import endpoint
from src.nextgen.sonarr_episode import VerifiedEpisode

MAX_RESPONSE = 4 * 1024 * 1024
MAX_RELEASES = 1000
MAX_ALIASES = 3
# A slow or busy Prowlarr gets two more tries before the search stops.
RETRY_DELAYS_SECONDS = (15, 60)
MAX_RETRY_AFTER_SECONDS = 120


class SearchReview(ValueError):
    reason_code = 'PROWLARR_SEARCH_REVIEW'
    def __init__(self): super().__init__('Prowlarr returned data that needs review.')


class SearchConfiguration(SearchReview):
    reason_code = 'PROWLARR_CONFIGURATION_INVALID'
    def __init__(self): ValueError.__init__(self, 'Configure Prowlarr before backfill search.')


class SearchUnauthorized(SearchConfiguration):
    reason_code = 'PROWLARR_AUTH_FAILED'
    def __init__(self): ValueError.__init__(self, 'Prowlarr rejected the API key.')


class SearchUnavailable(RuntimeError):
    reason_code = 'PROWLARR_UNAVAILABLE'
    retry_after = None      # seconds Prowlarr asked us to wait (429), if any
    def __init__(self): super().__init__('Prowlarr search is unavailable.')


class SearchTimeout(SearchUnavailable):
    reason_code = 'PROWLARR_SEARCH_TIMEOUT'
    def __init__(self): RuntimeError.__init__(self, 'Prowlarr search timed out.')


@dataclass(frozen=True)
class Options:
    base_url: str = field(repr=False)
    timeout_seconds: int = 60

    def __post_init__(self):
        try: object.__setattr__(self, 'base_url', endpoint(self.base_url))
        except ValueError: raise SearchConfiguration() from None
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise SearchConfiguration()

    @classmethod
    def capture(cls, configuration):
        return cls(configuration.value('prowlarr.base_url'),
                   configuration.value('prowlarr.timeout_seconds'))


@dataclass(frozen=True)
class DiscoveryPolicy:
    """Local search preferences; never override identity/source validation."""
    max_resolution: int = 1080
    min_seeders: int = 5
    rank_by: str = 'resolution'
    # acquisition.max_episode_mb (decimal MB). None: no size check here.
    max_episode_mb: int | None = None
    # acquisition.audio_check=japanese_english: require Dual/Multi Audio title hints.
    prefer_audio_tag: bool = False

    def __post_init__(self):
        if (type(self.max_resolution) is not int or self.max_resolution not in (720, 1080, 2160)
                or type(self.min_seeders) is not int or not 0 <= self.min_seeders <= 1_000_000
                or type(self.rank_by) is not str or self.rank_by not in ('resolution', 'seeders')
                or self.max_episode_mb is not None and (type(self.max_episode_mb) is not int
                                                         or self.max_episode_mb < 1)
                or type(self.prefer_audio_tag) is not bool):
            raise SearchConfiguration()

    @classmethod
    def capture(cls, configuration):
        return cls(configuration.value('prowlarr.max_resolution'),
                   configuration.value('prowlarr.min_seeders'),
                   configuration.value('prowlarr.rank_by'),
                   configuration.value('acquisition.max_episode_mb'),
                   wants_english(configuration.value('acquisition.audio_check')))

    def oversized(self, release):
        """Prowlarr reports the torrent's size; skip it before anything is sent."""
        return (self.max_episode_mb is not None and release.size > 0
                and release.size > self.max_episode_mb * 1_000_000)

    def accepts(self, release):
        return (release.eligible_for_handoff
                and 0 < release.resolution <= self.max_resolution
                and release.seeders is not None and release.seeders >= self.min_seeders
                and not self.oversized(release)
                and (not self.prefer_audio_tag or dual_audio_tagged(release.title)))

    def rank(self, releases):
        """Eligible releases first, then quality; ffprobe verifies actual audio."""
        rows = tuple(releases)
        def key(item):
            index, release = item
            preference = ((release.resolution, release.seeders or 0)
                          if self.rank_by == 'resolution'
                          else (release.seeders or 0, release.resolution))
            return (not self.accepts(release), -preference[0], -preference[1], index)
        return tuple(release for _, release in sorted(enumerate(rows), key=key))


@dataclass(frozen=True)
class Release:
    """One matching indexer result, not an acquisition authorization."""
    title: str
    indexer: str
    resolution: int
    seeders: int | None
    size: int
    tvdb_episode_id: int | None
    info_hash: str | None
    magnet_url: str | None = field(default=None, repr=False)
    eligible_for_handoff: bool = False
    # Plain "<title> - NN" naming of the verified AniDB episode, so verification
    # never depends on how the release group wrote its title.
    declared_title: str = ''


@dataclass(frozen=True)
class SearchResult:
    tvdb_series_id: int
    tvdb_episode_id: int | None
    candidates: tuple[Release, ...]


@dataclass(frozen=True)
class EpisodeTarget:
    """One AniDB episode and its own verified TVDB placement (see providers.backfill_catalog).

    TVDB fields are set only for SEARCHABLE targets.
    """
    tvdb_episode: int | None
    status: str                  # SEARCHABLE, NOT_AIRED or MAPPING_MISSING
    anidb_episode: int | None = None
    tvdb_episode_id: int | None = None
    absolute_episode: int | None = None
    tvdb_season: int | None = None


@dataclass(frozen=True)
class EpisodeCatalog:
    titles: tuple[str, ...]
    episodes: dict = field(repr=False)   # AniDB episode number -> EpisodeTarget
    english_title: str | None = None

    def target(self, number):
        # AniDB does not list this number yet; its cached record refreshes daily.
        return self.episodes.get(number) or EpisodeTarget(None, 'NOT_AIRED', number)


@dataclass(frozen=True)
class BackfillObservation:
    """Search summary only: no torrent link, verification, or acquisition permit."""
    number: int
    tvdb_episode_id: int | None
    status: str
    candidates: int = 0
    accepted: int = 0
    # In-memory only; magnets are never persisted from here.
    selection: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CandidateSelection:
    identity: VerifiedEpisode
    result: SearchResult = field(repr=False)
    release: Release = field(repr=False)
    policy: DiscoveryPolicy


RESOLUTION = re.compile(r'(?<![0-9])(480|540|576|720|1080|2160)[pi](?![A-Za-z0-9])', re.I)
UHD = re.compile(r'(?<![A-Za-z0-9])(?:4K|UHD)(?![A-Za-z0-9])', re.I)


def resolution(title):
    """Resolution declared in a release title, 0 when absent or contradictory."""
    found = {int(match.group(1)) for match in RESOLUTION.finditer(title)}
    if UHD.search(title): found.add(2160)
    return found.pop() if len(found) == 1 else 0


def _magnet_hash(uri):
    if not uri: return None
    if type(uri) is not str or len(uri) > 8192 or not uri.startswith('magnet:?'):
        return None
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
        xt = values.get('xt', [])
        if len(xt) != 1: return None
        match = re.fullmatch(r'urn:btih:([a-fA-F0-9]{40})', xt[0], flags=re.IGNORECASE)
        return match.group(1).lower() if match else None
    except ValueError:
        return None


def search_aliases(titles, *, english_title=None):
    """Up to three distinct Latin-script titles; AniDB main title first, then AniDB English title when known, then synonyms."""
    picked, seen = [], set()
    from src.nextgen.providers.parsing import norm
    candidates = []
    for title in titles:
        if (type(title) is not str or not 2 <= len(title) <= 200 or not title.isascii()
                or any(ord(c) < 32 for c in title)):
            continue
        key = norm(title)
        if key and key not in seen:
            seen.add(key)
            candidates.append(title)

    if not candidates:
        return ()

    # 1. Main title
    picked.append(candidates[0])

    # 2. English title only when AniDB gave one
    english_pick = None
    if english_title and type(english_title) is str and english_title.isascii():
        norm_eng = norm(english_title)
        for cand in candidates[1:]:
            if norm(cand) == norm_eng:
                english_pick = cand
                break
        if english_pick is None and 2 <= len(english_title) <= 200 and not any(ord(c) < 32 for c in english_title):
            if norm(english_title) != norm(picked[0]):
                english_pick = english_title

    if english_pick and english_pick not in picked:
        picked.append(english_pick)

    # 3. Remaining aliases up to MAX_ALIASES
    for cand in candidates[1:]:
        if cand not in picked:
            picked.append(cand)
        if len(picked) == MAX_ALIASES: break

    return tuple(picked)


def declared_title(titles, anidb_episode):
    """A plain title that VERIFY_RELEASE reads as exactly this AniDB episode."""
    from src.nextgen.providers.release_mapping import declared_episode
    from src.nextgen.release_contracts import ReleaseNotReady
    for name in (*search_aliases(titles), 'Episode'):
        candidate = '%s - %02d' % (name, anidb_episode) if name != 'Episode' else 'Episode %02d' % anidb_episode
        try:
            if declared_episode(candidate) == ('anidb', None, anidb_episode): return candidate
        except (ReleaseNotReady, ValueError):
            continue
    return 'Episode %02d' % anidb_episode


def matches_episode(title, titles, target, season):
    """Whether a release title is exactly the target episode of this anime."""
    from src.nextgen.acquisition.release_titles import AnimeTitles, matches, parse_release
    return matches(parse_release(title), AnimeTitles.build(titles, season), target, season)


def _unavailable(headers):
    error = SearchUnavailable()
    try: value = headers.get('Retry-After') if headers is not None else None
    except AttributeError: value = None
    if type(value) is str and re.fullmatch(r'[0-9]{1,6}', value.strip()):
        error.retry_after = max(1, int(value.strip()))
    return error


class Client:
    """GET-only Prowlarr transport plus a provider-backed episode catalog."""
    def __init__(self, options, api_key, *, catalog, transport=None, policy=None, sleep=time.sleep):
        if (type(options) is not Options or type(api_key) is not str or not api_key
                or len(api_key) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in api_key)
                or not callable(catalog)):
            raise SearchConfiguration()
        if policy is not None and type(policy) is not DiscoveryPolicy: raise SearchConfiguration()
        self.options, self._key = options, api_key
        self.catalog = catalog
        self.logger = None
        self.policy = policy or DiscoveryPolicy()
        self._sleep = sleep
        self.snapshots = []
        self._plan_id = None
        self._search_mode = 'initial'
        self._current_anidb_id = None
        self._current_tvdb_series_id = None
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
        self._transport = transport or (lambda request, timeout: opener.open(request, timeout=timeout))

    def set_context(self, plan_id=None, search_mode='initial'):
        self._plan_id = plan_id
        self._search_mode = search_mode

    @classmethod
    def from_context(cls, configuration, secrets, *, catalog, transport=None):
        try: key = secrets.prowlarr_api_key()
        except ValueError: raise SearchConfiguration() from None
        return cls(Options.capture(configuration), key, catalog=catalog, transport=transport,
                   policy=DiscoveryPolicy.capture(configuration))

    def _get(self, query, *, search_type='search', params=None):
        """One search; timeouts, 429 and 5xx get two more tries (15 s, then 60 s).

        A 429 Retry-After header replaces the wait, capped at two minutes.
        Authentication and invalid responses are never retried.
        """
        for attempt, delay in enumerate((*RETRY_DELAYS_SECONDS, None), 1):
            try:
                return self._get_once(query, search_type=search_type, params=params)
            except SearchUnavailable as exc:      # includes SearchTimeout
                if delay is None: raise
                if exc.retry_after is not None: delay = min(exc.retry_after, MAX_RETRY_AFTER_SECONDS)
                if self.logger is not None:
                    self.logger.warning('Prowlarr search failed (%s); try %s of %s in %s s',
                                        exc.reason_code, attempt + 1,
                                        len(RETRY_DELAYS_SECONDS) + 1, delay)
                self._sleep(delay)

    def _get_once(self, query, *, search_type='search', params=None):
        if type(query) is not str or not 1 <= len(query) <= 256:
            raise SearchReview()
        query_params = {'query': query, 'type': search_type, 'limit': 100}
        if params: query_params.update(params)
        url = (self.options.base_url + '/api/v1/search?' + urllib.parse.urlencode(query_params))
        request = urllib.request.Request(url, method='GET', headers={
            'X-Api-Key': self._key, 'Accept': 'application/json',
            'User-Agent': 'AnimeDiscoveryNextGen/1'})
        started = time.monotonic()
        if self.logger is not None:
            self.logger.info('Prowlarr search started: query=%r type=%s timeout_seconds=%s',
                             query, search_type, self.options.timeout_seconds)
        try:
            with self._transport(request, self.options.timeout_seconds) as response:
                code = response.getcode()
                if code in (401, 403): raise SearchUnauthorized()
                if code == 429 or code >= 500: raise _unavailable(getattr(response, 'headers', None))
                if code != 200: raise SearchReview()
                raw = response.read(MAX_RESPONSE + 1)
                if type(raw) is not bytes or len(raw) > MAX_RESPONSE: raise SearchReview()
                try: payload = _json(raw)
                except ValueError: raise SearchReview() from None
        except urllib.error.HTTPError as exc:
            code, headers = exc.code, exc.headers
            exc.close()
            if code in (401, 403): raise SearchUnauthorized() from None
            if code == 429 or code >= 500: raise _unavailable(headers) from None
            raise SearchReview() from None
        except TimeoutError:
            raise SearchTimeout() from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError): raise SearchTimeout() from None
            raise SearchUnavailable() from None
        except (OSError, http.client.HTTPException):
            raise SearchUnavailable() from None
        if type(payload) is not list or len(payload) > MAX_RELEASES:
            raise SearchReview()
        if self.logger is not None:
            self.logger.info('Prowlarr search answered: results=%s elapsed_seconds=%.3f',
                             len(payload), time.monotonic() - started)
        return payload

    def _search(self, catalog, season, target, *, tried=(), target_hash=None):
        """All matching releases for one searchable episode, ranked by policy."""
        from src.nextgen.acquisition.release_titles import AnimeTitles, search_queries, read_release, matches
        from src.nextgen.storage.prowlarr_snapshots import sanitize_text
        prepared = AnimeTitles.build(catalog.titles, season)
        english_title = getattr(catalog, 'english_title', None)
        queries = search_queries(catalog.titles, prepared, target, season=season,
                                 english_title=english_title, prefer_audio=self.policy.prefer_audio_tag)
        found, seen = [], set()
        tried_set = {h.lower() for h in tried if h}

        captured_queries = []
        captured_results = []
        counts_dispositions = {
            'UNMATCHED_TITLE_OR_EPISODE': 0,
            'MISSING_DUAL_AUDIO_HINT': 0,
            'MISSING_INFO_HASH': 0,
            'RESOLUTION_OUT_OF_POLICY': 0,
            'EPISODE_SIZE_TOO_LARGE': 0,
            'INSUFFICIENT_SEEDERS': 0,
            'PASSES_POLICY': 0,
        }
        raw_count = 0
        raw_dual_audio_count = 0
        matched_dual_audio_count = 0
        unmatched_count = 0
        matched_count = 0
        passes_policy_count = 0
        newly_actionable_count = 0

        snapshot = {
            'plan_id': getattr(self, '_plan_id', None),
            'anidb_id': getattr(self, '_current_anidb_id', None),
            'tvdb_series_id': getattr(self, '_current_tvdb_series_id', None),
            'season': season,
            'episode': target.tvdb_episode,
            'tvdb_episode_id': target.tvdb_episode_id,
            'anidb_episode': target.anidb_episode,
            # season/episode are the target's TVDB placement; anidb_episode is the window key.
            'numbering': {'season': 'tvdb', 'episode': 'tvdb', 'anidb_episode': 'anidb'},
            'search_mode': getattr(self, '_search_mode', 'initial'),
            'target': {
                'plan_id': getattr(self, '_plan_id', None),
                'anidb_id': getattr(self, '_current_anidb_id', None),
                'tvdb_series_id': getattr(self, '_current_tvdb_series_id', None),
                'season': season,
                'episode': target.tvdb_episode,
                'tvdb_episode_id': target.tvdb_episode_id,
                'anidb_episode': target.anidb_episode,
                'anime_titles': list(catalog.titles),
            },
            'policy': {
                'max_resolution': self.policy.max_resolution,
                'min_seeders': self.policy.min_seeders,
                'rank_by': self.policy.rank_by,
                'max_episode_mb': self.policy.max_episode_mb,
                'prefer_audio_tag': self.policy.prefer_audio_tag,
            },
            'queries': captured_queries,
            'results': captured_results,
            'counts': {},
            'status': 'COMPLETE',
            'error': None,
            'truncated': False,
        }
        self.snapshots.append(snapshot)

        row_number = 0
        seen_keys = {}
        for query_idx, query in enumerate(queries, 1):
            q_start = time.monotonic()
            query_matched = 0
            query_policy_eligible = 0
            query_new_actionable = 0
            try:
                rows = self._get(query)
                q_elapsed = time.monotonic() - q_start
            except Exception as exc:
                q_elapsed = time.monotonic() - q_start
                status_code = getattr(exc, 'reason_code', exc.__class__.__name__)
                captured_queries.append({
                    'index': query_idx,
                    'query': query,
                    'search_type': 'search',
                    'result_count': 0,
                    'exact_matches': 0,
                    'policy_eligible': 0,
                    'new_actionable': 0,
                    'status': status_code,
                    'elapsed_seconds': round(q_elapsed, 3),
                })
                snapshot['status'] = 'PARTIAL'
                snapshot['error'] = status_code
                snapshot['counts'] = {
                    'raw': raw_count,
                    'unmatched': unmatched_count,
                    'matched': matched_count,
                    'passes_policy': passes_policy_count,
                    'new_eligible': passes_policy_count,
                    'newly_actionable': newly_actionable_count,
                    'raw_dual_audio': raw_dual_audio_count,
                    'matched_dual_audio': matched_dual_audio_count,
                    'dispositions': counts_dispositions,
                }
                raise

            for row in rows:
                row_number += 1
                raw_count += 1
                if type(row) is not dict:
                    raise SearchReview()
                title = row.get('title')
                title_clean = sanitize_text(title)
                indexer = sanitize_text(row.get('indexer', ''))[:256]
                protocol = row.get('protocol')
                seeders = row.get('seeders') if type(row.get('seeders')) is int and row['seeders'] >= 0 else None
                size = row.get('size') if type(row.get('size')) is int and row['size'] >= 0 else 0
                has_dual = dual_audio_tagged(title_clean)
                if has_dual:
                    raw_dual_audio_count += 1

                valid_title = (type(title) is str and 1 <= len(title) <= 2048
                               and not any(ord(c) < 32 or ord(c) == 127 for c in title))
                is_torrent = protocol in ('torrent', None)
                parsed, parse_reason = read_release(title_clean) if valid_title else (None, 'INVALID_TITLE')
                episode_hint = None
                if parsed is not None:
                    if parsed.season is not None:
                        episode_hint = f'S{parsed.season:02d}E{parsed.episode:02d}'
                    else:
                        episode_hint = f'E{parsed.episode:02d}'

                is_match = valid_title and is_torrent and matches(parsed, prepared, target, season)
                if not is_match:
                    unmatched_count += 1
                    if not valid_title:
                        primary_disp = 'INVALID_TITLE'
                        reason = 'invalid release title characters or length'
                    elif not is_torrent:
                        primary_disp = 'NON_TORRENT_PROTOCOL'
                        reason = f"protocol '{protocol}' is not torrent"
                    else:
                        primary_disp = 'UNMATCHED_TITLE_OR_EPISODE'
                        if parsed is not None:
                            reason = f"parsed episode {episode_hint}; requested S{season:02d}E{target.tvdb_episode:02d}; excluded before policy"
                        else:
                            reason = f"unparseable title ({parse_reason}); excluded before policy"
                    counts_dispositions[primary_disp] = counts_dispositions.get(primary_disp, 0) + 1
                    failed_checks = [primary_disp]
                    captured_results.append({
                        'row_number': row_number,
                        'query_index': query_idx,
                        'title': title_clean,
                        'indexer': indexer,
                        'protocol': protocol or 'torrent',
                        'seeders': seeders,
                        'size_bytes': size,
                        'resolution': resolution(title_clean),
                        'info_hash': None,
                        'info_hash_prefix': None,
                        'has_dual_audio_hint': has_dual,
                        'parsed_episode_hint': episode_hint,
                        'matches_episode': False,
                        'is_duplicate': False,
                        'duplicate_of': None,
                        'primary_disposition': primary_disp,
                        'failed_checks': failed_checks,
                        'reason': reason,
                    })
                    continue

                matched_count += 1
                query_matched += 1
                if has_dual:
                    matched_dual_audio_count += 1

                raw_hash = row.get('infoHash')
                info_hash = (raw_hash.lower() if type(raw_hash) is str
                             and re.fullmatch('[a-fA-F0-9]{40}', raw_hash) else None)
                magnet = None
                magnet_conflict = False
                for source in (row.get('magnetUrl'), row.get('guid')):
                    source_hash = _magnet_hash(source)
                    if source_hash is None: continue
                    if info_hash is not None and source_hash != info_hash:
                        magnet_conflict = True
                        break
                    info_hash, magnet = source_hash, source
                    break
                if info_hash is not None and magnet is None:
                    magnet = 'magnet:?xt=urn:btih:' + info_hash

                dedup_key = info_hash or title_clean
                is_duplicate = dedup_key in seen_keys
                dup_of = seen_keys.get(dedup_key)
                if not is_duplicate:
                    seen_keys[dedup_key] = row_number

                row_failed_checks = []
                detailed_reasons = []

                if magnet_conflict:
                    row_failed_checks.append('MAGNET_HASH_CONFLICT')
                    detailed_reasons.append('conflicting info hash and magnet URI')
                elif info_hash is None:
                    row_failed_checks.append('MISSING_INFO_HASH')
                    detailed_reasons.append('no usable info hash in release data')

                res = resolution(title_clean)
                if not (0 < res <= self.policy.max_resolution):
                    row_failed_checks.append('RESOLUTION_OUT_OF_POLICY')
                    if res > self.policy.max_resolution:
                        detailed_reasons.append(f'resolution {res}p exceeds limit {self.policy.max_resolution}p')
                    else:
                        detailed_reasons.append(f'resolution unrecognized ({res})')

                oversized = (self.policy.max_episode_mb is not None and size > 0
                             and size > self.policy.max_episode_mb * 1_000_000)
                if oversized:
                    row_failed_checks.append('EPISODE_SIZE_TOO_LARGE')
                    detailed_reasons.append(f'size {size // 1_000_000} MB exceeds limit {self.policy.max_episode_mb} MB')

                if seeders is None or seeders < self.policy.min_seeders:
                    row_failed_checks.append('INSUFFICIENT_SEEDERS')
                    if seeders is None:
                        detailed_reasons.append('seeders unknown')
                    else:
                        detailed_reasons.append(f'seeders {seeders} below minimum {self.policy.min_seeders}')

                if self.policy.prefer_audio_tag and not has_dual:
                    row_failed_checks.append('MISSING_DUAL_AUDIO_HINT')
                    detailed_reasons.append('missing Dual-Audio title hint')

                if is_duplicate:
                    primary_disp = 'DUPLICATE_RESULT'
                    counts_dispositions['DUPLICATE_RESULT'] = counts_dispositions.get('DUPLICATE_RESULT', 0) + 1
                    reason = f"duplicate of #{dup_of:03d} (same info hash)"
                    failed_checks = ['DUPLICATE_RESULT']
                elif row_failed_checks:
                    primary_disp = row_failed_checks[0]
                    counts_dispositions[primary_disp] = counts_dispositions.get(primary_disp, 0) + 1
                    reason = detailed_reasons[0]
                    if len(detailed_reasons) > 1:
                        reason += '; also: ' + '; '.join(detailed_reasons[1:])
                    failed_checks = row_failed_checks
                else:
                    primary_disp = 'PASSES_POLICY'
                    passes_policy_count += 1
                    counts_dispositions['PASSES_POLICY'] += 1
                    reason = 'passes policy checks (resolution, seeders, size, audio hint)'
                    failed_checks = ['PASSES_POLICY']
                    query_policy_eligible += 1
                    if info_hash and info_hash.lower() not in tried_set:
                        query_new_actionable += 1
                        newly_actionable_count += 1

                captured_results.append({
                    'row_number': row_number,
                    'query_index': query_idx,
                    'title': title_clean,
                    'indexer': indexer,
                    'protocol': protocol or 'torrent',
                    'seeders': seeders,
                    'size_bytes': size,
                    'resolution': resolution(title_clean),
                    'info_hash': info_hash,
                    'info_hash_prefix': info_hash[:8] if info_hash else None,
                    'has_dual_audio_hint': has_dual,
                    'parsed_episode_hint': episode_hint,
                    'matches_episode': True,
                    'is_duplicate': is_duplicate,
                    'duplicate_of': dup_of,
                    'primary_disposition': primary_disp,
                    'failed_checks': failed_checks,
                    'reason': reason,
                })

                if not magnet_conflict and not is_duplicate:
                    release = Release(
                        title_clean[:2048],
                        indexer[:256],
                        resolution(title_clean),
                        seeders,
                        size,
                        target.tvdb_episode_id,
                        info_hash,
                        magnet,
                        info_hash is not None,
                        declared_title(catalog.titles, target.anidb_episode)
                    )
                    found.append(release)

            captured_queries.append({
                'index': query_idx,
                'query': query,
                'search_type': 'search',
                'result_count': len(rows),
                'exact_matches': query_matched,
                'policy_eligible': query_policy_eligible,
                'new_actionable': query_new_actionable,
                'status': 'SUCCESS',
                'elapsed_seconds': round(q_elapsed, 3),
            })
            if self.logger is not None:
                self.logger.info(
                    'Prowlarr query [%s/%s] %r: raw=%s matched=%s eligible=%s actionable=%s in %.3fs',
                    query_idx, len(queries), query, len(rows), query_matched, query_policy_eligible, query_new_actionable, q_elapsed
                )

            if target_hash and any(r.info_hash == target_hash for r in found):
                break

        snapshot['counts'] = {
            'raw': raw_count,
            'unmatched': unmatched_count,
            'matched': matched_count,
            'passes_policy': passes_policy_count,
            'new_eligible': passes_policy_count,
            'newly_actionable': newly_actionable_count,
            'raw_dual_audio': raw_dual_audio_count,
            'matched_dual_audio': matched_dual_audio_count,
            'dispositions': counts_dispositions,
        }

        oversized_count = sum(self.policy.oversized(r) for r in found)
        if oversized_count and self.logger is not None:
            self.logger.info('Backfill episode %s: skipped %s release(s) over %s MB',
                             target.anidb_episode, oversized_count, self.policy.max_episode_mb)
        return self.policy.rank(found)

    def backfill(self, anidb_id, tvdb_series_id, numbers, *, reserved=(), reserved_placements=(),
                 verified_ids=None, select_candidates=False, tried_by_episode=None):
        """One observation per requested AniDB episode. A missing release never fails the page.

        Each target is searched and matched at its own verified TVDB placement,
        so one page may span TVDB seasons.
        """
        self._current_anidb_id = anidb_id
        self._current_tvdb_series_id = tvdb_series_id
        if (type(anidb_id) is not int or anidb_id <= 0 or type(tvdb_series_id) is not int
                or tvdb_series_id <= 0
                or type(numbers) is not tuple or not 1 <= len(numbers) <= 4
                or any(type(n) is not int or n <= 0 for n in numbers)
                or len(set(numbers)) != len(numbers)
                or type(select_candidates) is not bool):
            raise SearchReview()
        verified_ids = verified_ids or {}
        catalog = self.catalog(anidb_id, tvdb_series_id)
        output = []
        for number in numbers:
            target = catalog.target(number)
            if number in reserved or (target.status == 'SEARCHABLE' and
                    target.tvdb_season is not None and
                    (target.tvdb_season, target.tvdb_episode) in reserved_placements):
                output.append(BackfillObservation(number, verified_ids.get(number) or target.tvdb_episode_id,
                                                  'IN_PROGRESS'))
                continue
            if target.status != 'SEARCHABLE':
                output.append(BackfillObservation(number, target.tvdb_episode_id, target.status))
                continue
            if verified_ids.get(number) not in (None, target.tvdb_episode_id):
                output.append(BackfillObservation(number, target.tvdb_episode_id, 'MAPPING_MISSING'))
                continue
            tried = (tried_by_episode or {}).get(number, ())
            ranked = self._search(catalog, target.tvdb_season, target, tried=tried)
            accepted = sum(self.policy.accepts(r) for r in ranked)
            status = 'CANDIDATES' if ranked else 'NO_RESULTS'
            # A searchable episode with no Dual/Multi Audio releases is an
            # availability outcome, not a failed audio probe or review job.
            if (self.policy.prefer_audio_tag and not accepted
                    and not any(dual_audio_tagged(r.title) for r in ranked)):
                status = 'WAITING_FOR_DUB'
            choice = None
            if select_candidates and accepted:
                identity = VerifiedEpisode(tvdb_series_id, target.tvdb_episode_id,
                                           target.tvdb_season, target.tvdb_episode)
                choice = CandidateSelection(identity,
                    SearchResult(tvdb_series_id, target.tvdb_episode_id, ranked),
                    ranked[0], self.policy)
            output.append(BackfillObservation(number, target.tvdb_episode_id,
                          status, len(ranked), accepted, choice))
        return tuple(output)

    def search(self, anidb_id, episode, identity, *, tried=(), target_hash=None):
        """Ranked releases for one previously selected AniDB episode (candidate fallback)."""
        if type(identity) is not VerifiedEpisode or type(episode) is not int or episode <= 0:
            raise SearchReview()
        self._current_anidb_id = anidb_id
        self._current_tvdb_series_id = identity.tvdb_series_id
        catalog = self.catalog(anidb_id, identity.tvdb_series_id)
        target = catalog.target(episode)
        # The same AniDB target as the initial search, still at its selected placement.
        if (target.status != 'SEARCHABLE' or target.tvdb_episode_id != identity.tvdb_episode_id
                or (target.tvdb_season, target.tvdb_episode) != (identity.tvdb_season, identity.tvdb_episode)):
            raise SearchReview()
        return SearchResult(identity.tvdb_series_id, identity.tvdb_episode_id,
                            self._search(catalog, target.tvdb_season, target, tried=tried, target_hash=target_hash))
