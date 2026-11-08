"""Pure batch/window intersection matching current NextGen trial preferences.

The result is an advisory file-selection proposal. It cannot authorize a
download or staging; provider facts and hard budgets must be persisted first.
"""
from dataclasses import dataclass

from src.anime.acquisition.metadata import BatchInventory
from pathlib import PurePosixPath
from src.anime.batch.verification import VerifiedBatch
from src.anime.batch.selection import BatchPlanningError, VIDEO, file_episode_number
from src.anime.user_evaluation_policy import Window
from src.anime.history import integer


@dataclass(frozen=True)
class BatchWindowPlan:
    anchor_episode: int
    eligible_episodes: tuple[int, ...]
    present_episodes: tuple[tuple[int, int], ...]
    probe_episode: int
    probe_index: int
    remaining_indices: tuple[int, ...]
    skipped_indices: tuple[int, ...]


def plan_batch_user_evaluation_window(inventory, verified, *, window, anchor_episode):
    """Intersect trusted mapped episodes with current configured trial window.

    Unlike the older fixed E01-E06 planner, this handles backfill_count and
    following_count (including a verified later-episode trigger).
    """
    if type(inventory) is not BatchInventory or type(verified) is not VerifiedBatch:
        raise ValueError('Hash-verified batch inventory and provider verification required')
    if type(window) is not Window:
        raise ValueError('Current trial window required')
    integer(anchor_episode,positive=True)
    if (inventory.info_hash,inventory.info_sha256) != (verified.info_hash,verified.info_sha256):
        raise BatchPlanningError('Torrent fingerprint differs from provider proof')
    # Require a complete, valid verified-file mapping. The fixed six-episode
    # helper isn't called because a custom window can exclude E01 entirely.
    if not verified.files:
        raise BatchPlanningError('No verified regular episodes')
    by_index={f.index:f for f in inventory.files}
    seen_indices, seen_episodes = set(), set()
    for proof in verified.files:
        raw=by_index.get(proof.index)
        if (raw is None or raw.path!=proof.path or proof.tvdb_season!=verified.season
                or proof.index in seen_indices or proof.tvdb_episode in seen_episodes):
            raise BatchPlanningError('Batch episode proof mismatch or duplicate')
        seen_indices.add(proof.index)
        seen_episodes.add(proof.tvdb_episode)
    proofs={proof.index:proof for proof in verified.files}
    for file in inventory.files:
        if PurePosixPath(file.path).suffix.lower() in VIDEO:
            number=file_episode_number(file.path,verified.season)
            proof=proofs.get(file.index)
            if number is not None and (proof is None or proof.release_episode!=number):
                raise BatchPlanningError('Regular video has no exact provider proof')
            if number is None and proof is not None:
                raise BatchPlanningError('Special video cannot carry regular episode proof')
    if anchor_episode not in seen_episodes:
        raise BatchPlanningError('Verified trial anchor is not present in torrent')
    allowed=window.episodes(anchor_episode)
    selected=tuple(sorted((f.tvdb_episode,f.index) for f in verified.files
                          if f.tvdb_episode in allowed))
    if not selected:
        raise BatchPlanningError('Trial window selects no verified files')
    probe_ep,probe_idx=selected[0]
    wanted={i for _,i in selected}
    return BatchWindowPlan(anchor_episode,allowed,selected,probe_ep,probe_idx,
                           tuple(i for _,i in selected[1:]),
                           tuple(sorted(f.index for f in inventory.files if f.index not in wanted)))
