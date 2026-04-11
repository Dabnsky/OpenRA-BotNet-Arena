"""Reward computation scaffold based on tick-to-tick diffs.

High-level goal
---------------
Produce a stable reward signal without adding complex game-engine hooks.

Why diff-based reward
---------------------
- Maintainability: all reward tuning lives in Python.
- Simplicity: derive signal from already exported tick summaries.
- Iteration speed: change formulas without recompiling OpenRA.

This module is consumed by featurizer.py when generating training rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

from .parser import PlayerTickRow


@dataclass(slots=True)
class RewardConfig:
    """Configurable weights for dense proxy rewards.

    Start simple and keep values explicit in one place.
    """

    alpha_enemy_loss: float = 1.0
    beta_own_loss: float = 1.0
    actor_loss_weight: float = 25.0


def terminal_reward(win_state: str | None) -> float:
    """Map terminal win state to sparse reward.

    Contract:
    - win  -> +1
    - draw ->  0
    - loss -> -1

    TODO:
    - Confirm final win_state labels from your exporter.
    """

    if win_state is None:
        return 0.0

    value = win_state.strip().lower()

    if value in {"win", "won", "victory"}:
        return 1.0
    if value in {"draw", "tie"}:
        return 0.0
    if value in {"loss", "lost", "defeat"}:
        return -1.0

    return 0.0


def _loss_proxy(previous: PlayerTickRow, current: PlayerTickRow, actor_loss_weight: float) -> float:
    """Compute simple loss proxy from hp and actor-count drops.

    Purpose:
    convert raw state deltas into a dense scalar suitable for shaping.
    """

    hp_drop = max(0, previous.total_hp - current.total_hp)
    actor_drop = max(0, previous.actor_count - current.actor_count)
    return float(hp_drop) + actor_loss_weight * float(actor_drop)


def step_dense_reward(
    own_previous: PlayerTickRow,
    own_current: PlayerTickRow,
    enemy_previous: PlayerTickRow,
    enemy_current: PlayerTickRow,
    config: RewardConfig,
) -> float:
    """Compute one step of dense reward from own/enemy deltas.

    Connected flow:
    build_reward_series -> step_dense_reward -> _loss_proxy
    """

    own_loss = _loss_proxy(own_previous, own_current, config.actor_loss_weight)
    enemy_loss = _loss_proxy(enemy_previous, enemy_current, config.actor_loss_weight)
    return config.alpha_enemy_loss * enemy_loss - config.beta_own_loss * own_loss


def build_reward_series(
    own_timeline: Sequence[PlayerTickRow],
    enemy_timeline: Sequence[PlayerTickRow],
    final_win_state: str | None,
    config: RewardConfig,
) -> List[float]:
    """Build reward value per step for one player.

    Assumption:
    own_timeline and enemy_timeline are tick-aligned and sorted.

    Connected call chain:
    featurizer.build_training_rows
      -> build_reward_series
         -> step_dense_reward
            -> _loss_proxy
         -> terminal_reward
    """

    if len(own_timeline) != len(enemy_timeline):
        raise ValueError("Timelines must be same length for aligned reward diffing.")

    if len(own_timeline) < 2:
        return [terminal_reward(final_win_state)] if own_timeline else []

    rewards: List[float] = []

    for index in range(len(own_timeline) - 1):
        reward_t = step_dense_reward(
            own_previous=own_timeline[index],
            own_current=own_timeline[index + 1],
            enemy_previous=enemy_timeline[index],
            enemy_current=enemy_timeline[index + 1],
            config=config,
        )
        rewards.append(reward_t)

    rewards.append(terminal_reward(final_win_state))
    return rewards


def infer_enemy_player_ids(player_ids: Sequence[int], own_player_id: int) -> List[int]:
    """Helper for 1v1 assumptions.

    Purpose:
    keep matchmaking constraints explicit in code.

    For now, this returns all other IDs and caller picks first enemy in 1v1.
    """

    return [player_id for player_id in player_ids if player_id != own_player_id]
