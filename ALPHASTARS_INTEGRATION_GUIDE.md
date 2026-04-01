# AlphaStar Integration Implementation Guide

**Last Updated:** March 31, 2026  
**Target:** Offline dataset export for game state observation + action capture for AI training

---

## Overview

This document outlines the current data flow for OpenRA game state and actions, then describes the specific changes needed to build an AlphaStar-style training pipeline. Each major component section includes:

- **Current Flow:** how the system works today
- **New Flow:** what changes to make and why
- **Specific Changes:** concrete file modifications and new classes needed
- **Integration Points:** where to hook into engine logic

---

## Table of Contents

1. [Game Loop Engine](#1-game-loop-engine)
2. [World State Capture](#2-world-state-capture)
3. [Action/Order Capture](#3-actionorder-capture)
4. [Episode and Metadata Tracking](#4-episode-and-metadata-tracking)
5. [Storage and Serialization](#5-storage-and-serialization)
6. [Integration Layer](#6-integration-layer)
7. [Data Pipeline Configuration](#7-data-pipeline-configuration)
8. [Implementation Phases](#8-implementation-phases)

---

## 1. Game Loop Engine

### Current Flow

```
Game.cs Main Loop (InnerLogicTick)
  ├─ Ui.Tick()
  ├─ Cursor.Tick()
  ├─ OrderManager.TickImmediate()
  │   └─ SendImmediateOrders()
  │   └─ ReceiveAllOrdersAndCheckSync()
  │
  ├─ OrderManager.TryTick()
  │   ├─ SendOrders()
  │   ├─ ProcessOrders()  ← Orders applied here
  │   └─ LocalFrameNumber++
  │
  ├─ OrderGenerator.Tick(world)
  │   └─ Unsynced input generation
  │
  ├─ World.Tick()  ← Simulation step
  │   ├─ Actor.Tick() × n
  │   ├─ ITick traits × n
  │   ├─ IEffect.Tick() × n
  │   ├─ Frame-end actions
  │   └─ GameStateExporter.ExportGameState() [DISCARDED]
  │
  └─ World.TickRender(wr)
      └─ Render-side state updates
```

**Problem:** 
- Actions are processed but not logged
- World state is captured but discarded
- No episode/episode-boundary tracking
- No reward computation
- No connection to training infrastructure

**Key Fact:** At the point `World.Tick()` completes, the prior tick's orders have been processed, actor state is updated, and we have a clean post-action state snapshot.

### New Flow

```
Game.cs Main Loop (InnerLogicTick)
  ├─ [existing: Ui, Cursor, OM]
  │
  ├─ OrderManager.TryTick()
  │   ├─ [existing: SendOrders, ProcessOrders]
  │   └─ TrainingDataManager.CaptureOrders(processedOrders)  ← NEW
  │       └─ Buffers order metadata for this frame
  │
  ├─ [existing: OrderGenerator.Tick]
  │
  ├─ World.Tick()
  │   ├─ [existing: Actor ticks, trait ticks, effects]
  │   ├─ TrainingDataManager.CaptureWorldState(world)  ← NEW
  │   │   └─ Builds observation from post-step world
  │   └─ [REMOVE: GameStateExporter.ExportGameState() discarded]
  │
  └─ World.TickRender(wr)
```

After `World.Tick()` if episode not over:
```
TrainingDataManager.CreateTrainingStep()
  ├─ Merge captured orders with observation
  ├─ Assign action labels
  ├─ Compute rewards
  └─ Write to sink (file, network, memory)
```

On episode end (game over, disconnect, etc):
```
World.EndGame()
  └─ TrainingDataManager.TerminateEpisode()
      ├─ Finalize rewards
      ├─ Write terminal flag
      └─ Archive episode metadata
```

### Specific Changes

**File: `OpenRA.Game/World.cs`**

- **Remove:** Line 426: `readonly GameStateExporter gameStateExport = new GameStateExporter();`
- **Remove:** Line 471: `gameStateExport.ExportGameState(this);`
- **Add:** Field to hold training manager
  ```csharp
  private ITrainingDataCapture trainingDataCapture;
  ```
- **Add:** Constructor parameter or factory injection to set manager
- **Add:** In `Tick()` after existing logic, before frame-end-actions:
  ```csharp
  trainingDataCapture?.CaptureWorldState(this);
  ```
- **Add:** In `EndGame()`, after setting `IsGameOver`:
  ```csharp
  trainingDataCapture?.NotifyEpisodeEnd(this, EndReason.GameOver);
  ```

**File: `OpenRA.Game/Game.cs`**

- **Find:** `InnerLogicTick(OrderManager orderManager)` method
- **Add:** After `orderManager.TryTick()` returns true:
  ```csharp
  if (willTick && world != null)
  {
      // Notify training capture of orders that were processed this tick
      var trainingCapture = world.TrainingDataCapture;
      trainingCapture?.NotifyOrdersProcessed(orderManager.NetFrameNumber);
  }
  ```

---

## 2. World State Capture

### Current Flow

**File: `OpenRA.Game/GameStateExporter.cs`**

```csharp
public sealed class GameStateExporter
{
    private void gatherWorldData(World world)
        // Snapshots tick, players, actors, map
    
    private PlayerData gatherPlayerData(Player player)
        // Groups actors into units/buildings by name string match
        // Gets player name, faction
    
    private ActorData gatherActorData(Actor actor)
        // Position, health, owner, activity type name
    
    public string ExportGameState(World world)
        // Returns JSON, caller must store it
        // Called from World.Tick() but result is discarded
}
```

**Problems:**
- One-shot JSON dump; no structured record
- Actor classification is string-based, not trait-safe
- No visibility filtering
- No resource state, production queue, tech tree
- No action labels
- Position is cell-level only (`actor.Location`)
- No tactical features (fog, explored, occupancy grids)
- Result is discarded

### New Flow

Replace `GameStateExporter` with two layers:

#### Layer 1: Observation Builder (Trait-driven)

**New File: `OpenRA.Game/AI/TrainingObservationBuilder.cs`**

```csharp
public sealed class TrainingObservationBuilder
{
    // Builds structured observation from World
    
    public WorldObservation CaptureWorldState(World world, Player pov)
    {
        return new WorldObservation
        {
            Tick = world.WorldTick,
            Players = CapturePlayerStates(world),
            Entities = CaptureEntityList(world, pov),
            Maps = CaptureSpatialFeatures(world, pov),
            GlobalResources = CaptureGlobalState(world)
        };
    }
    
    private List<PlayerObservation> CapturePlayerStates(World world)
        // Per-player: name, faction, resources, technology, etc.
    
    private List<EntityObservation> CaptureEntityList(World world, Player pov)
        // Per-entity: id, position, hp, owner, traits metadata, queues, etc.
        // Visibility-filtered if pov != null
    
    private SpatialFeatures CaptureSpatialFeatures(World world, Player pov)
        // Terrain occupancy grid
        // Passability grid
        // Resource locations
        // Visibility/shroud per-player
        // Friendly/enemy unit/building occupancy masks
    
    private GlobalState CaptureGlobalState(World world)
        // Match duration, tech tree state, etc.
}
```

**Schema Classes: `OpenRA.Game/AI/TrainingDataStructures.cs`**

```csharp
public sealed class WorldObservation
{
    public int Tick { get; set; }
    public List<PlayerObservation> Players { get; set; }
    public List<EntityObservation> Entities { get; set; }
    public SpatialFeatures Maps { get; set; }
    public GlobalState GlobalResources { get; set; }
}

public sealed class EntityObservation
{
    public uint ActorId { get; set; }
    public string ActorType { get; set; }
    public (int X, int Y) Position { get; set; }          // Cell coords
    public (double X, double Y) CenterPosition { get; set; } // World coords
    public int Health { get; set; }
    public int MaxHealth { get; set; }
    public string Owner { get; set; }
    public int OwnerId { get; set; }
    public bool IsDead { get; set; }
    public string CurrentActivity { get; set; }
    
    // Trait-driven metadata
    public Dictionary<string, object> TraitState { get; set; }
        // fuel level, ammo, cargo, is-building, can-attack, etc.
}

public sealed class PlayerObservation
{
    public int PlayerId { get; set; }
    public string PlayerName { get; set; }
    public string Faction { get; set; }
    public int Resources { get; set; }
    public Dictionary<string, int> TechState { get; set; }
    public List<ProductionQueueStatus> ProductionQueues { get; set; }
}

public sealed class SpatialFeatures
{
    // Map-sized grids for ML consumption
    public byte[] Terrain { get; set; }                // Terrain type grid
    public byte[] Passability { get; set; }            // Cell passability
    public byte[] Resources { get; set; }              // Resource density
    public byte[] VisibleToPlayer { get; set; }        // Per-player visibility
    public byte[] FriendlyUnitPresence { get; set; }   // Occupancy grid
    public byte[] EnemyUnitPresence { get; set; }      // Occupancy grid
}

public sealed class GlobalState
{
    public int GameTick { get; set; }
    public int GameDuration { get; set; }
    public bool IsGameOver { get; set; }
    public Dictionary<string, object> CustomMetrics { get; set; }
}
```

#### Layer 2: Training Data Manager

**New File: `OpenRA.Game/AI/TrainingDataManager.cs`**

```csharp
public sealed class TrainingDataManager : ITrainingDataCapture
{
    private WorldObservation lastObservation;
    private List<ActionRecord> pendingActions;
    private TrainingEpisodeRecord currentEpisode;
    
    public void CaptureWorldState(World world)
    {
        // Build observation for current tick
        var builder = new TrainingObservationBuilder();
        lastObservation = builder.CaptureWorldState(world, world.LocalPlayer);
    }
    
    public void NotifyOrdersProcessed(int tick, List<Order> orders)
    {
        // Convert orders to action records
        foreach (var order in orders)
        {
            pendingActions.Add(new ActionRecord
            {
                Tick = tick,
                Order = order,
                // Parse order into structured action
            });
        }
    }
    
    public void CreateTrainingStep(World world, int tick)
    {
        // Combine observation + actions into a step
        var step = new TrainingStep
        {
            Tick = tick,
            Observation = lastObservation,
            Actions = pendingActions.ToList(),
            Reward = ComputeReward(world, lastObservation),
            Terminal = false
        };
        
        sink.Write(step);
        pendingActions.Clear();
    }
    
    public void NotifyEpisodeEnd(World world, string reason)
    {
        currentEpisode.EndTime = DateTime.UtcNow;
        currentEpisode.EndReason = reason;
        currentEpisode.FinalObservation = lastObservation;
        
        sink.WriteEpisodeEnd(currentEpisode);
    }
}
```

### Specific Changes

**Delete:** `OpenRA.Game/GameStateExporter.cs` (replace, don't keep)

**Create:** `OpenRA.Game/AI/TrainingDataStructures.cs`

**Create:** `OpenRA.Game/AI/TrainingObservationBuilder.cs`

**Create:** `OpenRA.Game/AI/TrainingDataManager.cs`

**File: `OpenRA.Game/World.cs`**

```csharp
// Add field
public ITrainingDataCapture TrainingDataCapture { get; private set; }

// In constructor, after other initialization:
public World(...)
{
    // ... existing code ...
    
    // Initialize training capture if requested
    if (ShouldCaptureTrainingData(type))
        TrainingDataCapture = new TrainingDataManager(new TrainingDataSink(...));
}
```

**File: `OpenRA.Game/World.cs` in `Tick()` method:**

Replace:
```csharp
while (frameEndActions.Count != 0)
    frameEndActions.Dequeue()(this);

gameStateExport.ExportGameState(this);
```

With:
```csharp
while (frameEndActions.Count != 0)
    frameEndActions.Dequeue()(this);

// Capture world state for training if enabled
TrainingDataCapture?.CaptureWorldState(this);
```

---

## 3. Action/Order Capture

### Current Flow

**File: `OpenRA.Game/Network/OrderManager.cs` in `ProcessOrders()`**

```csharp
void ProcessOrders()
{
    foreach (var (clientId, frameOrders) in pendingOrders)
    {
        var (frameNumber, orders) = frameOrders.Dequeue();
        
        if (orders == ClientDisconnected)
        {
            // Handle disconnect
            continue;
        }
        
        foreach (var order in orders.GetOrders(World))
        {
            UnitOrders.ProcessOrder(this, World, clientId, order);
            processClientOrders.Add(new ClientOrder { ... });
        }
    }
    
    // ... sync reporting ...
    ++NetFrameNumber;
}
```

**Problem:**
- Orders are processed but not logged in a training-friendly format
- No canonical action record per tick
- No mapping of order arguments to entity ids/positions
- Action source (which player) not cleanly tied to order metadata

### New Flow

Inject a training action recorder into the order processing pipeline.

**New File: `OpenRA.Game/AI/ActionRecorder.cs`**

```csharp
public sealed class ActionRecorder : IActionRecorder
{
    private List<ActionRecord> recordedActions = [];
    
    public void RecordOrder(int clientId, Order order, int tick, Player player)
    {
        var action = new ActionRecord
        {
            Tick = tick,
            PlayerId = clientId,
            PlayerName = player.PlayerName,
            OrderType = order.OrderString,
            Actor = order.Subject?.ActorID ?? 0,
            Target = ParseTarget(order),
            Queue = order.Queued,
            Raw = order.ToSaveFormat()  // Fallback: raw order for replay
        };
        
        recordedActions.Add(action);
    }
    
    public List<ActionRecord> GetAndClear()
    {
        var result = recordedActions;
        recordedActions = [];
        return result;
    }
}

public sealed class ActionRecord
{
    public int Tick { get; set; }
    public int PlayerId { get; set; }
    public string PlayerName { get; set; }
    public string OrderType { get; set; }
    public uint ActorId { get; set; }         // Selected/commanded actor
    public ActionTarget Target { get; set; }  // Can be position, actor id, or neither
    public bool Queue { get; set; }           // Was this queued?
    public string RawOrder { get; set; }      // Fallback for complex orders
}

public sealed class ActionTarget
{
    public ActionTargetType Type { get; set; }  // Position, Actor, None
    public (int X, int Y)? TargetPosition { get; set; }
    public uint? TargetActorId { get; set; }
}

public enum ActionTargetType { Position, Actor, None }
```

### Specific Changes

**Create:** `OpenRA.Game/AI/ActionRecorder.cs`

**File: `OpenRA.Game/World.cs`**

```csharp
// Add field
public IActionRecorder ActionRecorder { get; private set; }

// In constructor:
public World(...)
{
    // ... existing ...
    ActionRecorder = new ActionRecorder();
}
```

**File: `OpenRA.Game/Network/OrderManager.cs` in `ProcessOrders()`**

Add after processing each order, inside the order loop:

```csharp
foreach (var order in orders.GetOrders(World))
{
    UnitOrders.ProcessOrder(this, World, clientId, order);
    processClientOrders.Add(new ClientOrder { Client = clientId, Order = order });
    
    // NEW: Record for training
    var player = World.Players.FirstOrDefault(p => p.ClientIndex == clientId);
    if (player != null)
        World.ActionRecorder?.RecordOrder(clientId, order, NetFrameNumber, player);
}
```

**File: `OpenRA.Game/Game.cs` in `InnerLogicTick()`**

After `orderManager.TryTick()` succeeds:

```csharp
if (willTick && world != null)
{
    var actions = world.ActionRecorder?.GetAndClear() ?? [];
    world.TrainingDataCapture?.NotifyActionsThisFrame(actions);
}
```

---

## 4. Episode and Metadata Tracking

### Current Flow

**File: `OpenRA.Game/GameInformation.cs`**

```csharp
public sealed class GameInformation
{
    public string Mod { get; set; }
    public string Version { get; set; }
    public string MapUid { get; set; }
    public string MapTitle { get; set; }
    // ... player outcome tracking ...
    public int FinalGameTick { get; set; }
}
```

**Problem:**
- Episode metadata is scattered across `GameInformation` and replay metadata
- No training-specific episode identifier
- No reward signal structure
- No training metadata (start time, end reason, etc.)

### New Flow

Create a dedicated episode tracker that wraps `GameInformation`.

**New File: `OpenRA.Game/AI/TrainingEpisodeMetadata.cs`**

```csharp
public sealed class TrainingEpisodeMetadata
{
    public string EpisodeId { get; set; }                    // UUID for this match
    public DateTime StartTime { get; set; }
    public DateTime EndTime { get; set; }
    public string EndReason { get; set; }                    // "Win", "Defeat", "Disconnect", "Timeout"
    
    public string Map { get; set; }
    public string MapUid { get; set; }
    public string Mod { get; set; }
    public string ModVersion { get; set; }
    
    public List<EpisodePlayerRecord> Players { get; set; }   // One per player
    public int TotalTicks { get; set; }
    
    // Optional: replay file reference
    public string ReplayPath { get; set; }
    
    // Rewards and outcomes
    public Dictionary<int, float> FinalRewardPerPlayer { get; set; }
    public Dictionary<int, string> OutcomePerPlayer { get; set; }  // "Win", "Defeat", "Draw"
}

public sealed class EpisodePlayerRecord
{
    public int PlayerId { get; set; }
    public string PlayerName { get; set; }
    public string Faction { get; set; }
    public string PlayerType { get; set; }  // "Human", "AI", "Bot"
    public float FinalReward { get; set; }
    public string Outcome { get; set; }
}
```

**New File: `OpenRA.Game/AI/RewardComputer.cs`**

```csharp
public interface IRewardProvider
{
    float ComputeReward(World world, Player player, WorldObservation obs);
    void OnGameEnd(World world, Player player);
}

public sealed class DefaultRewardProvider : IRewardProvider
{
    private Dictionary<int, (int Health, int UnitsCount, int BuildingsCount)> lastState = [];
    
    public float ComputeReward(World world, Player player, WorldObservation playerObs)
    {
        float reward = 0f;
        
        // Dense shaping: reward resource deltas, unit creation/destruction
        var currentUnits = playerObs.Entities.Count(e => !IsBuilding(e) && e.Owner == player.PlayerName);
        var currentBuildings = playerObs.Entities.Count(e => IsBuilding(e) && e.Owner == player.PlayerName);
        var currentHealth = playerObs.Entities
            .Where(e => e.Owner == player.PlayerName)
            .Sum(e => e.Health);
        
        if (lastState.TryGetValue(player.ClientIndex, out var prev))
        {
            reward += (currentUnits - prev.UnitsCount) * 10f;          // Unit creation bonus
            reward += (currentBuildings - prev.BuildingsCount) * 50f;  // Building bonus
            reward += (currentHealth - prev.Health) * 0.1f;             // Health maintenance
        }
        
        lastState[player.ClientIndex] = (currentHealth, currentUnits, currentBuildings);
        
        return reward;
    }
    
    public void OnGameEnd(World world, Player player)
    {
        // Implement win bonus if player is not defeated
        if (player.WinState != WinState.Lost)
            // Win bonus will be added in TrainingDataManager
            _ = 1f; // placeholder
    }
    
    private bool IsBuilding(EntityObservation e)
    {
        // Check trait metadata or string pattern
        return e.TraitState.ContainsKey("Building");
    }
}
```

### Specific Changes

**Create:** `OpenRA.Game/AI/TrainingEpisodeMetadata.cs`

**Create:** `OpenRA.Game/AI/RewardComputer.cs`

**File: `OpenRA.Game/World.cs` in constructor**

```csharp
internal World(Map map, ModData modData, OrderManager orderManager, WorldType type)
{
    // ... existing ...
    
    // NEW: Initialize episode metadata
    this.episodeMetadata = new TrainingEpisodeMetadata
    {
        EpisodeId = Guid.NewGuid().ToString(),
        StartTime = DateTime.UtcNow,
        Map = map.Title,
        MapUid = map.Uid,
        Mod = modData.Manifest.Id,
        ModVersion = modData.Manifest.Metadata.Version
    };
}
```

**File: `OpenRA.Game/World.cs` in `EndGame()`**

```csharp
public void EndGame()
{
    if (!IsGameOver)
    {
        SetPauseState(true);
        IsGameOver = true;
        
        // NEW: Finalize episode metadata
        episodeMetadata.EndTime = DateTime.UtcNow;
        episodeMetadata.TotalTicks = WorldTick;
        episodeMetadata.EndReason = DetermineLossReason();
        episodeMetadata.FinalRewardPerPlayer = OrderManager.Connection is ReplayConnection
            ? new()
            : ComputeFinalRewards();
        
        foreach (var t in WorldActor.TraitsImplementing<IGameOver>())
            t.GameOver(this);
        
        gameInfo.FinalGameTick = WorldTick;
        TrainingDataCapture?.NotifyEpisodeEnd(episodeMetadata);
        
        GameOver();
    }
}
```

---

## 5. Storage and Serialization

### Current Flow

**Current Situation:**
- No structured training export
- Replays go to `OpenRA.Replays` format (custom binary)
- Game logs go to text files
- No training dataset format

### New Flow

Define pluggable storage backends that receive training steps.

**New File: `OpenRA.Game/AI/TrainingDataSinks.cs`**

```csharp
public interface ITrainingDataSink
{
    void WriteStep(TrainingStep step);
    void WriteEpisodeMetadata(TrainingEpisodeMetadata episode);
    void Flush();
    void Dispose();
}

public sealed class NDJsonFileSink : ITrainingDataSink
{
    private readonly string outputDir;
    private readonly string episodeId;
    private readonly StreamWriter stepWriter;
    
    public NDJsonFileSink(string outputDir, string episodeId)
    {
        this.outputDir = outputDir;
        this.episodeId = episodeId;
        
        Directory.CreateDirectory(outputDir);
        var stepFile = Path.Combine(outputDir, $"{episodeId}_steps.ndjson");
        stepWriter = new StreamWriter(stepFile, false);
    }
    
    public void WriteStep(TrainingStep step)
    {
        var json = JsonSerializer.Serialize(step);
        stepWriter.WriteLine(json);
    }
    
    public void WriteEpisodeMetadata(TrainingEpisodeMetadata episode)
    {
        var metaFile = Path.Combine(outputDir, $"{episode.EpisodeId}_meta.json");
        var json = JsonSerializer.Serialize(episode);
        File.WriteAllText(metaFile, json);
    }
    
    public void Flush()
    {
        stepWriter.Flush();
    }
    
    public void Dispose()
    {
        stepWriter?.Dispose();
    }
}

public sealed class ProtobufFileSink : ITrainingDataSink
{
    // Binary format for efficiency
    // Requires schema definition in .proto file
    // TODO: implement with protobuf-net
}

public sealed class NamedPipeSink : ITrainingDataSink
{
    // Streams to Python trainer or environment server
    // Real-time feedback during training
}
```

**New File: `OpenRA.Game/AI/TrainingStep.cs`**

```csharp
public sealed class TrainingStep
{
    public string EpisodeId { get; set; }
    public int Tick { get; set; }
    public WorldObservation Observation { get; set; }
    public List<ActionRecord> Actions { get; set; }
    public float Reward { get; set; }
    public bool Terminal { get; set; }
    public Dictionary<string, object> AgentInfo { get; set; }  // Per-player state
}
```

### Specific Changes

**Create:** `OpenRA.Game/AI/TrainingDataSinks.cs`

**Create:** `OpenRA.Game/AI/TrainingStep.cs`

**File: `OpenRA.Game/World.cs`**

```csharp
// Add field
private ITrainingDataSink trainingDataSink;

// In constructor, after episode metadata setup:
public World(...)
{
    // ... existing ...
    
    if (ShouldCaptureTrainingData(type))
    {
        string sinkType = gameSettings.TrainingDataSinkType ?? "ndjson";
        string outputDir = gameSettings.TrainingDataOutputDir ?? "training_data";
        
        trainingDataSink = sinkType switch
        {
            "ndjson" => new NDJsonFileSink(outputDir, episodeMetadata.EpisodeId),
            "protobuf" => new ProtobufFileSink(outputDir, episodeMetadata.EpisodeId),
            _ => throw new NotSupportedException($"Sink type: {sinkType}")
        };
        
        TrainingDataCapture = new TrainingDataManager(trainingDataSink, episodeMetadata);
    }
}

// In Dispose():
public void Dispose()
{
    TrainingDataCapture?.Flush();
    trainingDataSink?.Flush();
    trainingDataSink?.Dispose();
    
    // ... existing dispose logic ...
}
```

---

## 6. Integration Layer

### Current Flow

**Current Situation:**
- Game and training are completely separate systems
- No hooks to enable/disable training capture
- No configuration for training parameters
- No way to query training status

### New Flow

Create a centralized training integration interface.

**New File: `OpenRA.Game/AI/IGameTrainingIntegration.cs`**

```csharp
public interface IGameTrainingIntegration
{
    bool IsTrainingEnabled { get; }
    void Initialize(World world, GameTrainingSettings settings);
    void StepObservation(World world);
    void StepActions(List<ActionRecord> actions);
    void FinalizeEpisode(TrainingEpisodeMetadata metadata);
    void Dispose();
}

public sealed class GameTrainingIntegration : IGameTrainingIntegration
{
    public bool IsTrainingEnabled { get; private set; }
    
    private ITrainingDataCapture trainingCapture;
    private TrainingEpisodeMetadata currentEpisode;
    
    public void Initialize(World world, GameTrainingSettings settings)
    {
        IsTrainingEnabled = settings.Enabled;
        if (!IsTrainingEnabled) return;
        
        var sink = CreateSink(settings);
        var rewardProvider = CreateRewardProvider(settings);
        
        trainingCapture = new TrainingDataManager(sink, rewardProvider, world.LocalPlayer);
        currentEpisode = world.EpisodeMetadata;
    }
    
    public void StepObservation(World world)
    {
        trainingCapture?.CaptureWorldState(world);
    }
    
    public void StepActions(List<ActionRecord> actions)
    {
        trainingCapture?.NotifyActionsThisFrame(actions);
    }
    
    public void FinalizeEpisode(TrainingEpisodeMetadata metadata)
    {
        trainingCapture?.NotifyEpisodeEnd(metadata);
    }
    
    public void Dispose()
    {
        trainingCapture?.Flush();
    }
}

public sealed class GameTrainingSettings
{
    public bool Enabled { get; set; } = false;
    public string SinkType { get; set; } = "ndjson";  // ndjson, protobuf, pipe
    public string OutputDirectory { get; set; } = "training_data";
    public string RewardType { get; set; } = "default";  // default, sparse, custom
    public bool CaptureFullState { get; set; } = false;  // Also export unrevealed state
    public bool CapturePartialObservation { get; set; } = true;  // FOW-filtered
    public int CompressionLevel { get; set; } = 0;  // 0 = none, 1-9 = gzip levels
}
```

**File: `OpenRA.Game/Game.cs`**

```csharp
// Field
private IGameTrainingIntegration trainingIntegration;

// In StartGame() after OrderManager.StartGame():
private static void StartGame(Map map, WorldType type)
{
    // ... existing ...
    OrderManager.StartGame();
    
    // NEW: Initialize training if enabled
    var trainingSettings = new GameTrainingSettings
    {
        Enabled = Game.Settings.Game.EnableTrainingCapture,
        SinkType = Game.Settings.Game.TrainingDataSinkType,
        OutputDirectory = Game.Settings.Game.TrainingDataDirectory
    };
    
    trainingIntegration = new GameTrainingIntegration();
    trainingIntegration.Initialize(OrderManager.World, trainingSettings);
    
    // ... rest of method ...
}

// In InnerLogicTick(), after World.Tick():
if (orderManager.TryTick())
{
    // ... existing tick logic ...
    
    if (world != null)
    {
        trainingIntegration?.StepObservation(world);
        var actions = world.ActionRecorder?.GetAndClear() ?? [];
        trainingIntegration?.StepActions(actions);
    }
}

// In World.EndGame():
trainingIntegration?.FinalizeEpisode(world.EpisodeMetadata);
```

### Specific Changes

**Create:** `OpenRA.Game/AI/IGameTrainingIntegration.cs`

**File: `OpenRA.Game/Game.cs`**

Add field and initialization/teardown as noted above.

**File: `OpenRA.Game/Settings.cs` or equivalent**

Add training-related settings:
```csharp
public bool EnableTrainingCapture = false;
public string TrainingDataSinkType = "ndjson";
public string TrainingDataDirectory = "training_data";
```

---

## 7. Data Pipeline Configuration

### Current Flow

**Current Situation:**
- No configuration for training
- No way to selectively enable capture for certain games
- No replay-based training export

### New Flow

#### Configuration File

**File: `training_config.yaml` (in mod root or game root)**

```yaml
# Training Data Capture Configuration
# Used by OpenRA for offline or online training data export

training:
  enabled: true
  
  # Output format: ndjson, protobuf, named_pipe
  sink_type: "ndjson"
  output_directory: "./training_data"
  
  # Observation captures
  observation:
    # Full state (for debugging)
    capture_full_state: false
    
    # Player-visible fog-of-war filtered observation
    capture_partial_observation: true
    
    # Spatial grid resolution (0 = no grids)
    grid_resolution: 1
    
    # Include raw actor properties in entity list
    include_detailed_traits: true
  
  action:
    # Canonical action format
    format: "openra"  # or: "alphastar" for abstracted action space
  
  reward:
    # Reward computation type
    type: "default"
    
    # Dense shaping coefficients
    unit_creation_reward: 10.0
    unit_destruction_reward: -10.0
    building_creation_reward: 50.0
    building_destruction_reward: -50.0
    damage_dealt_reward: 0.5
    damage_taken_reward: -0.5
    
    # Terminal rewards
    win_bonus: 100.0
    loss_penalty: -100.0
  
  episode:
    # Metadata to include
    include_replay_path: true
    include_player_ratings: false
    include_map_hash: true

logging:
  # Training data logging verbosity
  level: "info"  # debug, info, warning, error
  dump_observations: false  # Write human-readable JSON alongside NDJSON
```

#### Programmatic API

**File: `OpenRA.Game/AI/TrainingConfigLoader.cs`**

```csharp
public sealed class TrainingConfigLoader
{
    public static GameTrainingSettings LoadFromFile(string configPath)
    {
        var yaml = MiniYaml.FromFile(configPath);
        var training = yaml.FirstOrDefault(n => n.Key == "training");
        
        if (training == null)
            return GameTrainingSettings.Disabled;
        
        var node = training.Value;
        
        return new GameTrainingSettings
        {
            Enabled = node.Value<bool>("enabled", false),
            SinkType = node.Value<string>("sink_type", "ndjson"),
            OutputDirectory = node.Value<string>("output_directory", "training_data"),
            CaptureFullState = node.Value<bool>("observation/capture_full_state", false),
            RewardType = node.Value<string>("reward/type", "default"),
            // ... parse remaining settings ...
        };
    }
}
```

#### Command-line Overrides

**File: `OpenRA.Game/Game.cs`**

```csharp
public static void ParseCommandLineArgs(string[] args)
{
    for (int i = 0; i < args.Length; i++)
    {
        if (args[i] == "--training-enabled")
            Settings.Game.EnableTrainingCapture = true;
        
        if (args[i] == "--training-output" && i + 1 < args.Length)
            Settings.Game.TrainingDataDirectory = args[++i];
        
        if (args[i] == "--training-config" && i + 1 < args.Length)
        {
            var settings = TrainingConfigLoader.LoadFromFile(args[++i]);
            Settings.Game.UpdateFromTrainingSettings(settings);
        }
    }
}
```

### Specific Changes

**Create:** `OpenRA.Game/AI/TrainingConfigLoader.cs`

**Create/Update:** `training_config.yaml` in repo root

**File: `OpenRA.Game/Settings.cs`**

Add training settings fields and update from loader.

**File: `OpenRA.Game/Game.cs`**

Call training config loader in initialization.

---

## 8. Implementation Phases

### Phase 1: Foundation (Weeks 1-2)

**Goal:** Establish core data capture infrastructure. No external API yet; focus on file export.

#### Tasks:

1. **Create data structures** (`TrainingDataStructures.cs`)
   - `WorldObservation`, `EntityObservation`, `PlayerObservation`
   - `ActionRecord`, `TrainingStep`
   - Basic schemas only; no spatial grids yet

2. **Create observation builder** (`TrainingObservationBuilder.cs`)
   - Iterate all actors
   - Build entity list per observation
   - Group by player
   - **No visibility filtering yet**

3. **Create action recorder** (`ActionRecorder.cs`)
   - Hook into `ProcessOrders()` in `OrderManager`
   - Record canonical order → action mapping
   - Implement in `World.Tick()`

4. **Create NDJSON sink** (`TrainingDataSinks.cs`)
   - Line-delimited JSON file writer
   - One step per line
   - Episode metadata file

5. **Integrate into World.Tick()** (`World.cs`)
   - Add capture calls
   - Remove old exporter
   - Add episode metadata tracking

6. **Add basic settings** (`Settings.cs`)
   - `EnableTrainingCapture` bool
   - `TrainingDataDirectory` path
   - Command-line args

7. **Test:**
   - Run a game single-player
   - Verify NDJSON files are created
   - Spot-check a few steps for correctness

**Success Criteria:** 
- NDJSON files written to disk for a single-player match
- Each step contains: observable actors, their properties, issued orders, tick number
- Episode metadata is complete

---

### Phase 2: Enrichment (Weeks 3-4)

**Goal:** Add spatial features, visibility filtering, and reward computation.

#### Tasks:

1. **Add spatial grids** (`TrainingObservationBuilder.cs`)
   - Occupancy grids (units, buildings)
   - Terrain/passability
   - Resource locations
   - Dimensions: Map size × map size

2. **Add visibility filtering** (`TrainingObservationBuilder.cs`)
   - `CaptureWorldState(world, playerPov)` variant
   - Filter entities by player shroud/fog
   - Export both full + partial views for training

3. **Add reward provider** (`RewardComputer.cs`)
   - Dense shaping: unit creation, destruction, health deltas
   - Terminal rewards: win/loss bonus/penalty
   - Integration into `TrainingDataManager`

4. **Add trait-aware entity features** (`TrainingObservationBuilder.cs`)
   - Query traits for unit state:
     - `IHealth` → hp, max hp
     - `IMove` → speed, movement state
     - `IAttackBase` → attack range, cooldown
     - `IRefinery` → resources harvested
   - Store in `TraitState` dict

5. **Add episode metadata** (`TrainingEpisodeMetadata.cs`)
   - Capture game end reason
   - Record final rewards per player
   - Record outcomes

6. **Test observation variety:**
   - Multi-player game
   - Verify visibility masks differ per player
   - Verify spatial grids are correct
   - Verify rewards accumulate reasonably

**Success Criteria:**
- Full vs partial observations exported, with correct visibility filtering
- Spatial grids included and queryable
- Reward signals flow through training steps
- Episode metadata records outcomes

---

### Phase 3: External Integration (Weeks 5-6)

**Goal:** Support external environment servers and named pipes.

#### Tasks:

1. **Implement named pipe sink** (`TrainingDataSinks.cs`)
   - Connect to named pipe
   - Stream observations + actions in real-time
   - Graceful connection loss handling

2. **Define environment API** (new file: `OpenRA.Game/AI/IEnvironmentServer.cs`)
   ```csharp
   public interface IEnvironmentServer
   {
       void Reset();
       (Observation, Info) GetObservation();
       void Step(Action action);
       (Observation, Reward, Done, Info) GetStepResult();
   }
   ```

3. **Implement gRPC or REST endpoint** (optional: `OpenRA.Launcher/EnvironmentService.cs`)
   - Expose game as environment to external agent

4. **Build Python client** (outside repo: `alphastars_training/environment_client.py`)
   - Connect to named pipe or gRPC
   - Serialize/deserialize observations
   - Translate AlphaStar actions → OpenRA orders

5. **Test:**
   - Python trainer connects via pipe
   - Issues orders to game
   - Receives observations
   - Game progresses correctly

**Success Criteria:**
- External process can observe game state
- External process can issue orders
- Game responds and advances deterministically

---

### Phase 4: Optimization (Week 7)

**Goal:** Binary encoding, compression, batching for scale.

#### Tasks:

1. **Implement protobuf sink** (`TrainingDataSinks.cs`)
   - Define `.proto` schema for observations, actions, episodes
   - Compile with protobuf-net
   - Compare file sizes vs NDJSON

2. **Add compression** (`TrainingDataSinks.cs`)
   - Optional gzip compression on output
   - Configurable level

3. **Add batching**
   - Collect N steps, write batch header
   - Reduces I/O overhead

4. **Benchmark:**
   - Compare NDJSON vs protobuf file sizes
   - Measure serialization time
   - Profile memory usage

**Success Criteria:**
- Protobuf format available and working
- ≥ 50% file size reduction vs NDJSON (typical for binary)
- Negligible performance overhead

---

### Phase 5: Replay Export (Week 8)

**Goal:** Mine training data from existing replay files.

#### Tasks:

1. **Create replay processor** (`OpenRA.Game/AI/ReplayDataExporter.cs`)
   - Open a `.replay` file
   - Reinitialize game state from replay header
   - Iterate through orders frame-by-frame
   - Call same observation/action capture pipeline
   - Write to training sink

2. **Create batch processor** (CLI tool: `OpenRA.Launcher/export_training_data.exe`)
   - Accept directory of replay files
   - Export each to training dataset
   - Merge into unified dataset

3. **Test:**
   - Export 10 example replays
   - Verify step counts match tick counts
   - Verify reproducibility

**Success Criteria:**
- Replay files can be converted to training datasets
- Determinism verified (same replay → same data twice)

---

### Phase 6: Integration Tests & Documentation (Week 9+)

**Goal:** Complete test coverage and runbooks.

#### Tasks:

1. Write integration tests (`OpenRA.Test/`)
   - Single-player game capture
   - Multi-player game capture
   - Replay export
   - Schema validation

2. Write usage documentation
   - How to enable training for a game
   - How to configure capture format
   - How to export replays
   - How to run external trainer

3. Write developer documentation
   - Architecture overview
   - How to add custom reward providers
   - How to implement custom sinks
   - Extension points

4. Package examples

---

## Quick Reference: Files to Create/Modify

### New Files to Create

```
OpenRA.Game/AI/
├── TrainingDataStructures.cs        # Observation, action, episode schemas
├── TrainingObservationBuilder.cs    # Builds obs from World
├── TrainingDataManager.cs           # Coordinates capture
├── ActionRecorder.cs                # Records orders → actions
├── RewardComputer.cs                # Computes rewards
├── TrainingDataSinks.cs             # File/pipe output
├── TrainingStep.cs                  # Step record type
├── TrainingEpisodeMetadata.cs       # Episode info
├── IGameTrainingIntegration.cs      # Integration interface
├── TrainingConfigLoader.cs          # YAML config loader
└── [Phase 3+] ReplayDataExporter.cs # Replay mining
```

### Files to Modify

```
OpenRA.Game/
├── World.cs                         # Add episode metadata, capture hooks
├── Game.cs                          # Add training initialization in startup loop
├── Network/OrderManager.cs          # Hook into ProcessOrders
├── Settings.cs                      # Add training settings
└── [Phase 5+] GameInformation.cs    # Link to episode metadata
```

### New Configuration

```
/
├── training_config.yaml             # Training capture settings
```

---

## Design Decisions Recap

### Observation Format: **Hybrid (Entity List + Spatial Grids)**
- Entity list for ML models that want discrete object recognition
- Spatial grids for CNN-style architectures
- Export both; trainer can choose which to use

### Visibility: **Both Full + Partial**
- Full state for debugging and post-hoc analysis
- Partial (FOW-filtered) for realistic training
- Compute both; enable/disable per training run

### Action Space: **OpenRA-Native First**
- Start with canonical engine orders
- Later add abstracted multi-head agents
- Easier to bootstrap; can layer ML-side abstractions

### Storage: **Pluggable Sinks**
- NDJSON for debugging and low scale
- Protobuf/binary for production and datasets
- Named pipes for real-time agent integration

### Reward: **Dense Shaping + Terminal**
- Default provider logs primitives (damage, unit counts, resources)
- Trainer can compute custom rewards offline
- Terminal rewards (win/loss) applied at episode end

### Integration: **Centralized Manager**
- Single `IGameTrainingIntegration` facade
- Initialized at game start
- Pluggable implementations (offline vs online)

---

## How to Use This Document

1. **Read Phases 1-2** to understand the initial implementation
2. **For each phase, follow:**
   - \[Current Flow\] to understand what exists
   - \[New Flow\] to understand the design
   - \[Specific Changes\] to implement concrete modifications
3. **After each phase, run tests** using the success criteria
4. **Iterate:** Phases are not airtight; move incrementally
5. **Reference [Quick Reference section](#quick-reference-files-to-createmodify) for file locations**

---

## Next Steps

1. Create the data structure file: `TrainingDataStructures.cs`
2. Create the observation builder: `TrainingObservationBuilder.cs`
3. Modify `World.cs` and `OrderManager.cs` to call these new components
4. Test with a single-player game
5. Verify NDJSON output contains expected fields

Once Phase 1 is complete, move to Phase 2 for spatial grids and visibility.

