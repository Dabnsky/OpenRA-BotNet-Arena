"""Minimal behavior-cloning training scaffold for OpenRA adapter.

High-level goal
---------------
Create the smallest maintainable loop that proves:
JSONL -> parsed episodes -> features/actions/rewards -> trainable model.

Scope
-----
This scaffold intentionally favors clarity over completeness.
You can start with a tiny baseline and replace each TODO in small increments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import numpy as np

from .featurizer import FeatureConfig, TrainingRow, build_training_rows
from .parser import parse_episode
from .reward_diff import RewardConfig


@dataclass(slots=True)
class TrainConfig:
    """Top-level training config for first-pass experiments."""

    data_dir: Path
    max_files: int | None = None
    validation_fraction: float = 0.2
    random_seed: int = 7


def list_episode_files(data_dir: Path, max_files: int | None = None) -> List[Path]:
    """Discover JSONL files in deterministic order.

    Connected flow:
    main -> list_episode_files -> load_training_rows
    """

    files = sorted(data_dir.glob("*.jsonl"))
    if max_files is not None:
        files = files[:max_files]
    return files


def load_training_rows(paths: Sequence[Path]) -> List[TrainingRow]:
    """Load and featurize all episodes.

    Connected flow:
    load_training_rows
      -> parse_episode
      -> build_training_rows

    Current default behavior:
    - process every player timeline found in each episode
    - keep exceptions visible so data issues are explicit during development
    """

    reward_config = RewardConfig()
    feature_config = FeatureConfig()

    all_rows: List[TrainingRow] = []

    for path in paths:
        episode = parse_episode(path)

        for player_id in episode.player_timelines:
            all_rows.extend(
                build_training_rows(
                    episode=episode,
                    player_id=player_id,
                    reward_config=reward_config,
                    feature_config=feature_config,
                )
            )

    return all_rows


def rows_to_arrays(rows: Sequence[TrainingRow]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert row objects into numpy arrays.

    Returns:
    - X: feature matrix [N, D]
    - y: action indices [N]
    - w: sample weights [N] (uses simple reward-aware weighting)
    """

    if not rows:
        raise ValueError("No training rows found. Check parser/featurizer outputs.")

    x = np.asarray([row.features for row in rows], dtype=np.float32)
    y = np.asarray([row.action_index for row in rows], dtype=np.int32)

    # Simple weighting idea:
    # - baseline weight 1.0
    # - emphasize positive reward samples slightly
    # TODO: tune or replace after baseline is stable.
    w = np.asarray([1.0 + max(0.0, row.reward) for row in rows], dtype=np.float32)

    return x, y, w


def split_train_val(
    x: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    validation_fraction: float,
    random_seed: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split arrays into train and validation partitions.

    Keep this deterministic for reproducible debugging.
    """

    rng = np.random.default_rng(random_seed)
    indices = np.arange(x.shape[0])
    rng.shuffle(indices)

    split_index = int((1.0 - validation_fraction) * len(indices))
    train_idx = indices[:split_index]
    val_idx = indices[split_index:]

    return (
        x[train_idx],
        y[train_idx],
        w[train_idx],
        x[val_idx],
        y[val_idx],
        w[val_idx],
    )


def train_baseline_model(
    x_train: np.ndarray,
    y_train: np.ndarray,
    w_train: np.ndarray,
):
    """Train a tiny baseline classifier.

    TODO choices (pick one):
    - sklearn.linear_model.LogisticRegression
    - sklearn.neural_network.MLPClassifier
    - your preferred lightweight classifier

    Why a tiny baseline:
    prove data pipeline health before adding model complexity.
    """

    # TODO: implement selected baseline model.
    # Example pseudo-code:
    # from sklearn.linear_model import LogisticRegression
    # model = LogisticRegression(max_iter=200)
    # model.fit(x_train, y_train, sample_weight=w_train)
    # return model

    raise NotImplementedError("Implement your first baseline model here.")


def evaluate_model(model, x_val: np.ndarray, y_val: np.ndarray) -> dict:
    """Evaluate model with minimal diagnostics.

    TODO:
    - report top-1 accuracy
    - optionally report per-action precision/recall later
    """

    # TODO pseudo-code:
    # y_pred = model.predict(x_val)
    # accuracy = float((y_pred == y_val).mean())
    # return {"accuracy": accuracy}

    raise NotImplementedError("Implement baseline evaluation here.")


def main(config: TrainConfig) -> None:
    """End-to-end orchestration function.

    Connected call chain:
    main
      -> list_episode_files
      -> load_training_rows
      -> rows_to_arrays
      -> split_train_val
      -> train_baseline_model
      -> evaluate_model
    """

    episode_files = list_episode_files(config.data_dir, max_files=config.max_files)
    rows = load_training_rows(episode_files)

    x, y, w = rows_to_arrays(rows)

    (
        x_train,
        y_train,
        w_train,
        x_val,
        y_val,
        _w_val,
    ) = split_train_val(
        x=x,
        y=y,
        w=w,
        validation_fraction=config.validation_fraction,
        random_seed=config.random_seed,
    )

    model = train_baseline_model(
        x_train=x_train,
        y_train=y_train,
        w_train=w_train,
    )

    metrics = evaluate_model(model, x_val=x_val, y_val=y_val)
    print("Validation metrics:", metrics)


if __name__ == "__main__":
    # TODO: replace with argparse if preferred.
    default_config = TrainConfig(
        data_dir=Path("ml-export"),
        max_files=10,
    )
    main(default_config)
