"""Verify one declared regular episode using AniDB, explicit mapping and TVDB."""
from src.anime.providers.errors import (
    ProviderInvalidResponse, ProviderAuthenticationFailed,
    ProviderUnavailable, ProviderConfigurationError,
)
from src.anime.providers.http import Clients, Credentials, Options
from src.anime.providers.identity import ClientPool, delivery_title
from src.anime.providers.release_mapping import declared_episode, forward_mappings
from src.anime.batch.recognition import release_scope_hint
from src.anime.release_contracts import VerifiedRelease, ReleaseNotReady, ReleaseConflict
from src.anime.worker import blocking
import inspect
from contextlib import closing
from src.anime.storage.recognition import recognized_episode
from src.anime.storage.migrations import _open, LATEST_VERSION


def make_verifier(options, credentials, *, client_pool=None, client_factory=Clients, batch_verifier=None):
    if (type(options) is not Options and not callable(options)) or not callable(credentials):
        raise ValueError("release provider composition is incomplete")
    if batch_verifier is not None and not callable(batch_verifier):
        raise ValueError('async batch verifier required')
    pool=client_pool or ClientPool(client_factory)

    def verify(source,configuration,cancelled):
        if cancelled.is_set(): raise ReleaseNotReady()
        try: title=delivery_title(source)
        except ValueError: raise ReleaseNotReady("RELEASE_TITLE_UNCLEAR") from None
        origin,season,number=declared_episode(title)
        # Exact previously verified episode placement is reusable. There is no
        # release approval here: the normal trial, reservation, and acquisition
        # gates run unchanged, and an unknown mapping still needs providers.
        with closing(_open(configuration.value("storage.database_path"),
                           readonly=True,timeout_ms=5000)) as known_db:
            if known_db.execute("PRAGMA user_version").fetchone()[0]==LATEST_VERSION:
                hit=recognized_episode(known_db,aid=source.anidb_id,
                    tid=source.tvdb_series_id,origin=origin,season=season,number=number)
                if hit is not None:
                    if cancelled.is_set():raise ReleaseNotReady()
                    return VerifiedRelease(source,hit["anidb_episode_number"],
                        hit["anidb_episode_id"],hit["tvdb_season"],
                        hit["tvdb_episode"],hit["tvdb_episode_id"])
        actual=options(configuration) if callable(options) else options
        try: secret=credentials()
        except ProviderConfigurationError: secret=None
        if type(secret) is not Credentials:
            if origin!="anidb": raise ProviderAuthenticationFailed()
            secret=None
        client=pool.get(configuration.value("storage.database_path"),actual)
        catalog=client.episodes(source.anidb_id)
        if origin=="anidb":
            if number not in catalog: raise ReleaseNotReady("RELEASE_ANIDB_EPISODE_MISSING")
            try:
                mappings=forward_mappings(client.mapping_xml(),source.anidb_id,source.tvdb_series_id,catalog)
            except (ReleaseNotReady, ReleaseConflict):
                mappings={}
            target=mappings.get(number)
            if target is None or secret is None:
                if cancelled.is_set(): raise ReleaseNotReady()
                return VerifiedRelease(source,number,catalog[number],None,None,None)
        else:
            mappings=forward_mappings(client.mapping_xml(),source.anidb_id,source.tvdb_series_id,catalog)
            candidates=[n for n,target in mappings.items() if target==(season,number)]
            if not candidates:
                # Only explicitly mapped regular AniDB episodes can verify
                # TVDB S00. AniDB's separate special-numbering type is not
                # interchangeable with the TVDB Season 0 number.
                raise ReleaseNotReady("RELEASE_SPECIAL_UNSUPPORTED" if season == 0
                                      else "RELEASE_EPISODE_MAPPING_MISSING")
            if len(candidates)!=1: raise ReleaseNotReady("RELEASE_EPISODE_MAPPING_AMBIGUOUS")
            number=candidates[0]
            target=mappings[number]
        # Detect merged mappings even in the forward case. A single declared
        # episode never authorizes an ambiguous split/merged coverage assertion.
        if sum(value==target for value in mappings.values())!=1:
            if origin=="anidb":
                if cancelled.is_set(): raise ReleaseNotReady()
                return VerifiedRelease(source,number,catalog[number],None,None,None)
            raise ReleaseConflict()
        seen=set()
        matches=[]
        total=0
        try:
            for page in range(20):
                if cancelled.is_set(): raise ReleaseNotReady()
                records,has_next=client.episode_page(source.tvdb_series_id,page,secret)
                total+=len(records)
                if total>10000: raise ProviderInvalidResponse()
                for eid,record_season,record_number in records:
                    if eid in seen: raise ProviderInvalidResponse()
                    seen.add(eid)
                    if (record_season,record_number)==target: matches.append(eid)
                if not has_next: break
            else: raise ProviderInvalidResponse()
        except (ProviderUnavailable, ProviderInvalidResponse, ProviderAuthenticationFailed):
            if origin=="anidb":
                if cancelled.is_set(): raise ReleaseNotReady()
                return VerifiedRelease(source,number,catalog[number],None,None,None)
            raise
        if not matches:
            if origin=="anidb":
                if cancelled.is_set(): raise ReleaseNotReady()
                return VerifiedRelease(source,number,catalog[number],None,None,None)
            raise ReleaseNotReady("RELEASE_TVDB_EPISODE_MISSING")
        if len(matches)!=1:
            if origin=="anidb":
                if cancelled.is_set(): raise ReleaseNotReady()
                return VerifiedRelease(source,number,catalog[number],None,None,None)
            raise ReleaseConflict()
        if cancelled.is_set(): raise ReleaseNotReady()
        return VerifiedRelease(source,number,catalog[number],target[0],target[1],matches[0])

    async def call(source,configuration,cancelled,*,claim=None):
        if batch_verifier is not None:
            try:title=delivery_title(source)
            except ValueError:raise ReleaseNotReady('RELEASE_TITLE_UNCLEAR') from None
            if release_scope_hint(title).kind in ('season_pack','mixed_pack'):
                if claim is not None and 'claim' in inspect.signature(batch_verifier).parameters:
                    return await batch_verifier(source,configuration,cancelled,claim=claim)
                return await batch_verifier(source,configuration,cancelled)
        return await blocking(verify,source,configuration,cancelled)
    return call
