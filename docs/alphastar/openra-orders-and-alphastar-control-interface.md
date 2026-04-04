# OpenRA Orders and AlphaStar Control Interface

## Purpose
This document focuses on how human orders are issued in OpenRA online games, and what changes are needed for AlphaStar to interface with OpenRA and issue orders as a player equivalent to a human participant.

Constraint: this plan assumes **no files are added to or modified in `alphastar/`**.

---

## 1) How Human Orders Flow in OpenRA Today

## 1.1 Input to Order generation
- Human mouse/keyboard interactions are handled in UI controllers (notably `WorldInteractionControllerWidget`).
- The active `IOrderGenerator` converts interaction context into one or more `Order` objects.
- Default unit interaction behavior is implemented by `UnitOrderGenerator`.

## 1.2 Order object model
`Order` carries:
- `OrderString` (operation/opcode)
- `Subject` actor (issuer)
- `Target` (actor/cell/terrain/frozen actor)
- queueing/modifier semantics (`Queued`, extra fields)
- optional grouped actors and payload fields (`ExtraData`, `TargetString`, etc.)

## 1.3 Network and simulation path
- Orders are submitted via `World.IssueOrder(...)` → `OrderManager.IssueOrder(...)`.
- `OrderManager` batches/sends/receives synchronized order packets.
- `UnitOrders.ProcessOrder(...)` and trait-level `IResolveOrder` handlers apply effects.
- `IValidateOrder` guards validity in synchronized play.

This means OpenRA already has a robust, deterministic command bus that AlphaStar should reuse.

---

## 2) Existing Bot Integration Surface

OpenRA already supports bots via `IBot` / `IBotInfo`:
- `Activate(Player p)`
- `QueueOrder(Order order)`
- built-in implementations (e.g. `ModularBot`) queue and issue orders through `world.IssueOrder(...)`.

This is the ideal insertion point for AlphaStar control: treat AlphaStar as another `IBot` implementation that emits standard `Order` objects.

---

## 3) Recommended Control Architecture

Use a **Bot Adapter** pattern:

1. `AlphaStarBot` (new C# trait implementing `IBot`) lives on the player actor.
2. It gathers current player-observation features each control interval.
3. It calls an inference backend (local process, gRPC, or shared memory).
4. It decodes model actions into legal `Order` instances.
5. It enqueues via `QueueOrder(...)` and issues through existing `world.IssueOrder(...)` path.

No custom command path should bypass `OrderManager`.

The inference backend and all model I/O adaptation should live in OpenRA-side or external adapter code, not in `alphastar/`.

---

## 4) Action Space Mapping (Human-like)

Design action heads around OpenRA order semantics, not raw mouse events:

- **Action type head:** maps to `OrderString` subset (move, attack, stop, guard, build, rally, production, support power, stance, etc.).
- **Selection head:** actor/group selector over controllable entities.
- **Target head:** target actor ID or map cell.
- **Queue/modifier head:** shift/ctrl/alt-like behavior where relevant.
- **Arguments head:** production item IDs, stance enum, support-power specifics.

Start with a reduced action set to guarantee legal decoding, then expand.

---

## 5) Observation Interface Needed for Inference

For online play, expose at each decision step:
- visible entities (ally/enemy/neutral) with fog constraints
- own economy/resources/power
- production queues and cooldowns
- selected group/control group features
- map context/minimap tensors
- game clock/tick

Observations used for online inference should match the offline training schema to avoid train/serve skew.

---

## 6) Concrete OpenRA Changes Required

## 6.1 New bot trait + modules
Add new files under `OpenRA.Mods.Common/Traits/Player/`:
- `AlphaStarBotInfo` : `TraitInfo`, `IBotInfo`
- `AlphaStarBot` : `IBot`, likely `ITick`

Responsibilities:
- pull current observations
- throttle decision frequency (e.g., every N ticks)
- request action from model backend
- convert to legal `Order` objects
- queue safely

## 6.2 Order legality gate
Implement a centralized order validator/decoder utility:
- confirms actor ownership and target validity
- enforces queue semantics
- rejects impossible orders before enqueue

This should leverage existing `IIssueOrder`/`IOrderTargeter` logic where possible.

## 6.3 Inference transport
Add one transport first (simplest: local process IPC or localhost gRPC):
- request: serialized observation + context IDs
- response: action struct
- timeout behavior: fallback policy (`no-op`, `stop`, or scripted safe action)

Implementation location: `OpenRA-BotNet-Arena/tools/openra_alphastar_adapter/` (or equivalent external folder), keeping the nested AlphaStar repo unchanged.

## 6.4 Multiplayer fairness controls
To behave like a human participant:
- decision rate cap (actions per second)
- optional reaction delay distribution
- no hidden-information access
- no out-of-band map omniscience

---

## 7) Human-Equivalent Play Constraints

AlphaStar must obey the same constraints as online humans:
- only sees legal player observation space
- only issues legal orders through `OrderManager`
- obeys net frame pacing and latency limits
- cannot mutate game state directly

If these hold, AlphaStar functions as a standard client-controlled player from the simulation’s perspective.

---

## 8) Suggested Rollout

### Step A: Offline mirror
- Record human order streams and state.
- Train behavior cloning with reduced action vocabulary via external wrapper scripts that invoke AlphaStar entrypoints.

### Step B: Sandbox bot games
- Run AlphaStarBot in local/skirmish matches.
- Validate no crashes, illegal order rates, and timing behavior.

### Step C: Controlled online tests
- Private server matches with telemetry logging.
- Evaluate fairness, win/loss, action distribution, and desync rate.

### Step D: Ladder-ready hardening
- add robust fallbacks, watchdogs, and strict schema/version checks.

---

## 9) Key Files in This Context

- Input/order generation:
  - `OpenRA.Mods.Common/Widgets/WorldInteractionControllerWidget.cs`
  - `OpenRA.Mods.Common/Orders/UnitOrderGenerator.cs`
  - `OpenRA.Game/Orders/IOrderGenerator.cs`
- Order transport/processing:
  - `OpenRA.Game/Network/OrderManager.cs`
  - `OpenRA.Game/Network/Order.cs`
  - `OpenRA.Game/Network/UnitOrders.cs`
- Bot interfaces:
  - `OpenRA.Game/Traits/TraitsInterfaces.cs` (`IBot`, `IBotInfo`, `IIssueOrder`, `IOrderTargeter`)
  - `OpenRA.Mods.Common/Traits/Player/ModularBot.cs`

---

## 10) Definition of Done

- AlphaStar issues only standard `Order` objects through existing synchronization code.
- Bot operates under fog-of-war and human-like action pacing constraints.
- End-to-end online match runs without desync/crash and with full telemetry.
- Offline and online interfaces share the same schema and action vocabulary.
