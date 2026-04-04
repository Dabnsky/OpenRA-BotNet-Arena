# OpenRA → AlphaStar Data Extraction Change Plan

## Purpose
This document describes how to add a robust data extraction pipeline from OpenRA so game data can be used to train AlphaStar workflows **without adding or modifying files inside the nested `alphastar` repo** (offline first, online-compatible later).

---

## 1) Current Architecture Snapshot

### OpenRA (as implemented in this workspace)
- Core simulation state is in `World` and progresses in `Game.InnerLogicTick(...)`.
- Orders are queued and synchronized in `OrderManager` (`IssueOrder`, `SendOrders`, `ProcessOrders`, `TryTick`).
- Network/replay packets are interpreted in `UnitOrders.ProcessOrder(...)`.
- Human input path is:
  1. UI widgets (e.g. `WorldInteractionControllerWidget`) create mouse/key interactions,
  2. `IOrderGenerator` / `UnitOrderGenerator` resolves interactions into `Order` instances,
  3. `World.IssueOrder(...)` forwards to `OrderManager`.
- Replay recording currently stores order packets (`ReplayRecorder`) and metadata, not a dense ML-ready tensor dataset.
- A custom `GameStateExporter` exists in this repo, but it is currently a coarse JSON snapshot utility and not yet a deterministic, schema-versioned training data pipeline.

### AlphaStar Unplugged (nested `alphastar` repo)
- Training expects serialized episodes in TFRecord with feature specs matching converter settings.
- Offline training (`OfflineTFRecordDataSource`) consumes episodic sequences and emits learner inputs (`observation`, `step_type`, behavior/prev features).
- Dataset generation pipeline in DeepMind code is SC2/PySC2 converter-driven, but the same high-level pattern can be reused:
  - replay/event source → per-step feature conversion → episodic serialization → data source reader.

---

## 2) Integration Strategy (Recommended)

Use a **two-stage adapter architecture**:

1. **OpenRA side (C#):** emit deterministic, schema-versioned per-step records + per-order records.
2. **External adapter side (Python, outside `alphastar`):** convert those records into model-ready tensors and launch AlphaStar training scripts using runtime config/flags only.

This avoids tight coupling to SC2-specific converter assumptions and lets you evolve OpenRA features independently.

---

## 3) Data Contract You Need

Define a versioned dataset contract, e.g. `openra_dataset_v1`:

- `episode_id`
- `tick`
- `player_id`
- `step_type` (`FIRST`/`MID`/`LAST`)
- `observation`:
  - fog-limited visible entities
  - own entities full stats
  - minimap/resource/power summaries
  - selected units + control groups
  - tech/build queue state
- `action`:
  - raw order opcode (`OrderString`)
  - argument payload (target cell/actor/tag, queued/modifiers)
  - grouped actor IDs / subject ID
- `reward` (optional initially; can be sparse outcome reward)
- `outcome` (win/loss/draw)
- `metadata` (map UID, mod version, rules hash, build hash)

Keep this in a shared schema doc before coding.

---

## 4) OpenRA Changes Required

## 4.1 Add deterministic capture hooks (core)
Primary insertion points:
- `OpenRA.Game/Network/OrderManager.cs`
  - hook after orders are accepted/processed each frame
  - capture authoritative order stream per client/frame
- `OpenRA.Game/Game.cs` (`InnerLogicTick`)
  - capture world observation snapshots at stable tick boundaries
- `OpenRA.Game/World.cs`
  - helper APIs to materialize player-perspective observations (respect shroud/fog)

Implementation notes:
- Capture from simulation-authoritative state, not UI transient state.
- Store both:
  - action events (orders)
  - aligned state snapshots (before/after order application; pick one and standardize)
- Add strict schema version field to every output record.

## 4.2 Replace ad-hoc exporter with pluggable recorder
Evolve `OpenRA.Game/GameStateExporter.cs` into:
- `IGameDataRecorder` interface with methods like:
  - `OnEpisodeStart(...)`
  - `OnTick(...)`
  - `OnOrdersProcessed(...)`
  - `OnEpisodeEnd(...)`
- concrete implementations:
  - `JsonlGameDataRecorder` (debug)
  - `BinaryGameDataRecorder` (production throughput)

## 4.3 Add config + launch toggles
Add runtime flags/settings:
- `--ml-export=true|false`
- output directory
- sample rate (every tick, every N ticks)
- included players (human only / all)

## 4.4 Preserve determinism + performance
- Perform capture in unsynced side effects only after authoritative state is known.
- Batch buffered writes to avoid frame stalls.
- Never mutate gameplay state from recorder code.

## 4.5 Replay backfill mode
Add offline replay conversion mode:
- read `.orarep`
- reconstruct world progression
- emit the same `openra_dataset_v1` records

This is critical for scaling historical data without requiring live matches.

---

## 5) External Adapter (No `alphastar` Repo Changes)

## 5.1 Add a separate adapter package
Create a standalone Python package outside `alphastar`, e.g.:
- `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/`

Responsibilities:
- load OpenRA episode files
- map OpenRA fields to tensor structures expected by AlphaStar runtime
- emit training/eval batches for wrapper scripts

## 5.2 Use AlphaStar as-is via script wrappers
Do not edit `alphastar` source files. Instead:
- call `alphastar/unplugged/scripts/train.py` and `evaluate.py` from wrapper scripts
- pass all settings through command-line config overrides
- keep OpenRA-specific preprocessing and dataset shaping in the external adapter

## 5.3 Observation/action adapters outside `alphastar`
Create adapters in the external package:
- observation featurizer (`openra_to_model_obs.py`)
- action decoder (`model_to_openra_action.py`)

These should be version-coupled to `openra_dataset_v1` and fail fast on mismatch.

---

## 6) Phased Delivery Plan

### Phase 1 (MVP)
- Record per-tick player observations + issued orders to JSONL.
- Add replay-to-JSONL backfill tool.
- Add external Python reader that can train a simple behavior cloning baseline without touching `alphastar` code.

### Phase 2
- Switch to binary/TFRecord-like high-throughput format.
- Add richer features (queues, control groups, production timers).
- Add stratified dataset partitioning by map/faction/MMR bracket.

### Phase 3
- Add online inference bridge for live play (see second document).
- Integrate evaluator loop for self-play / bot-vs-human ladder evaluation via external wrappers.

---

## 7) Risks and Mitigations

- **Schema drift across OpenRA versions:** include `schema_version`, `mod_version`, `rules_hash` in each episode.
- **Leakage of hidden information:** enforce per-player fog-limited views when creating observations.
- **Training instability from sparse labels:** begin with imitation learning on frequent action categories.
- **I/O bottlenecks:** write in batched chunks and compress asynchronously.

---

## 8) Minimal File-Level Change List

### OpenRA
- `OpenRA.Game/Network/OrderManager.cs` (order capture hooks)
- `OpenRA.Game/Game.cs` (tick boundary capture scheduling)
- `OpenRA.Game/World.cs` (player-perspective observation builder)
- `OpenRA.Game/GameStateExporter.cs` (refactor into recorder subsystem)
- (new) `OpenRA.Game/ML/*` (schema + recorder implementations)

### External adapter (outside `alphastar`)
- (new) `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/openra_data_source.py`
- (new) `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/openra_features.py`
- (new) `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/openra_actions.py`
- (new) `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/train_wrapper.py`

---

## 9) Definition of Done

- You can export full episodes from OpenRA with aligned `(observation_t, action_t, step_type_t)` tuples.
- You can train AlphaStar codepath through external wrappers/adapters without adding or modifying files under `alphastar/`.
- Replays and live games produce identical schema output.
- Hidden-information constraints are validated by tests.
