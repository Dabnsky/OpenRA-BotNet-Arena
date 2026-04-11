"""Feature and label construction scaffold for OpenRA adapter.

High-level goal
---------------
Convert parsed timelines and action records into fixed-shape model-ready rows.

How this aligns with the goal
-----------------------------
- parser.py: turns raw JSONL into structured episode data.
- reward_diff.py: adds reward targets from aligned timeline diffs.
- this file: produces numeric vectors and action labels.
- train_bc.py: consumes these rows for learning.

Design principle
----------------
Start with tiny, stable feature vectors and extend only when metrics plateau.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

from .parser import ParsedEpisode, PlayerTickRow
from .reward_diff import RewardConfig, build_reward_series, infer_enemy_player_ids


NO_OP_ACTION = "NoOp"

# TODO: Replace with discovered order vocabulary from your collected action logs.
ACTION_VOCAB: List[str] = [
    "Move",
    "Attack",
    "Stop",
    "Guard",
    NO_OP_ACTION,
]


@dataclass(slots=True)
class TrainingRow:
    """One training row emitted by build_training_rows.

    Fields are intentionally plain and explicit for easy inspection/debugging.
    """

    tick: int
    player_id: int
    features: List[float]
    action_index: int
    reward: float


@dataclass(slots=True)
class FeatureConfig:
    """Normalization constants for initial feature set.

    Keep constants centralized and versioned to avoid hidden preprocessing drift.
    """

    max_actor_count: float = 500.0
    max_total_hp: float = 50000.0


def action_to_index(action_record: Mapping[str, Any] | None, action_vocab: Sequence[str]) -> int:
    """Map one action record to vocab index.

    If no action exists at tick t, map to NoOp.

    Expected exporter shape:
    action_record["action"]["order_string"]
    """

    if action_record is None:
        return action_vocab.index(NO_OP_ACTION)

    action_payload = action_record.get("action", {})
    order_string = str(action_payload.get("order_string", NO_OP_ACTION))

    try:
        return action_vocab.index(order_string)
    except ValueError:
        return action_vocab.index(NO_OP_ACTION)


def make_feature_vector(
    own_tick: PlayerTickRow,
    enemy_tick: PlayerTickRow,
    config: FeatureConfig,
) -> List[float]:
    """Construct minimal fixed-size feature vector for one step.

    Current vector (4 dims):
    [own_actor_count, own_total_hp, enemy_actor_count, enemy_total_hp]

    Normalization keeps scales controlled for simple baseline models.
    """

    own_actor_count = own_tick.actor_count / config.max_actor_count
    own_total_hp = own_tick.total_hp / config.max_total_hp
    enemy_actor_count = enemy_tick.actor_count / config.max_actor_count
    enemy_total_hp = enemy_tick.total_hp / config.max_total_hp

    return [
        float(own_actor_count),
        float(own_total_hp),
        float(enemy_actor_count),
        float(enemy_total_hp),
    ]


def pick_action_for_tick(actions_at_tick: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Select one supervised action label for a tick.

    Minimal policy for now:
    - choose first action at tick if any
    - else None -> NoOp

    TODO:
    - Later, support multi-action ticks with sequence labels.
    """

    return actions_at_tick[0] if actions_at_tick else None


def build_training_rows(
    episode: ParsedEpisode,
    player_id: int,
    reward_config: RewardConfig,
    feature_config: FeatureConfig,
    action_vocab: Sequence[str] = ACTION_VOCAB,
) -> List[TrainingRow]:
    """Build training rows for one player from one parsed episode.

    Connected call chain:
    train_bc.load_training_rows
      -> build_training_rows
         -> infer_enemy_player_ids
         -> build_reward_series
            -> step_dense_reward (reward_diff)
         -> make_feature_vector
         -> action_to_index

    Assumes 1v1 for now by selecting the first non-own player id.
    """

    own_timeline = episode.player_timelines.get(player_id, [])
    if not own_timeline:
        return []

    enemy_ids = infer_enemy_player_ids(list(episode.player_timelines.keys()), own_player_id=player_id)
    if not enemy_ids:
        return []

    enemy_timeline = episode.player_timelines[enemy_ids[0]]

    # TODO: replace with real extraction once episode_end includes win/loss label.
    final_win_state: str | None = None

    rewards = build_reward_series(
        own_timeline=own_timeline,
        enemy_timeline=enemy_timeline,
        final_win_state=final_win_state,
        config=reward_config,
    )

    rows: List[TrainingRow] = []

    for index, own_tick in enumerate(own_timeline):
        enemy_tick = enemy_timeline[index]
        tick = own_tick.tick

        actions_at_tick = episode.actions_by_tick.get(tick, [])
        selected_action = pick_action_for_tick(actions_at_tick)

        row = TrainingRow(
            tick=tick,
            player_id=player_id,
            features=make_feature_vector(own_tick, enemy_tick, feature_config),
            action_index=action_to_index(selected_action, action_vocab),
            reward=rewards[index] if index < len(rewards) else 0.0,
        )
        rows.append(row)

    return rows
