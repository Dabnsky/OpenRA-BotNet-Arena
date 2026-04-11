#pragma warning disable IDE0055
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using OpenRA.Traits;

namespace OpenRA
{
	public sealed class GameStateExporter
	{
		public static GameStateExporter Instance { get; } = new();

		const string SchemaVersion = "openra_dataset_v1";

		bool configLoaded;
		bool enabled;
		int sampleEveryTicks = 8;
		string outputDirectory;

		StreamWriter writer;
		string episodeId;
		string mapUid;
		int lastObservedTick = -1;

		int tick;
		Player[] players = [];
		List<Actor> actorList = [];
		Map map;

		class PlayerData
		{
			public string Name { get; set; }
			public string Faction { get; set; }
			public List<ActorData> Units { get; set; } = [];
			public List<ActorData> Buildings { get; set; } = [];
		}

		class ActorData
		{
			public uint Id { get; set; }
			public string Type { get; set; }
			public object Position { get; set; }
			public int Health { get; set; }
			public bool IsDead { get; set; }
			public string Owner { get; set; }
			public string CurrentAction { get; set; }
		}

		List<PlayerData> playerDataList = [];

		GameStateExporter() { }

		void LoadConfig()
		{
			if (configLoaded)
				return;

			configLoaded = true;
			enabled = Environment.GetEnvironmentVariable("OPENRA_ML_EXPORT") == "1";

			if (int.TryParse(Environment.GetEnvironmentVariable("OPENRA_ML_EXPORT_EVERY"), out var every) && every > 0)
				sampleEveryTicks = every;

			outputDirectory = Environment.GetEnvironmentVariable("OPENRA_ML_EXPORT_DIR");
			if (string.IsNullOrWhiteSpace(outputDirectory))
				outputDirectory = Path.Combine(Platform.SupportDir, "ml-export");
		}

		bool EnsureWriter(World world)
		{
			LoadConfig();

			if (!enabled || world == null || world.Type != WorldType.Regular)
				return false;

			var worldMapUid = world.Map?.Uid ?? "unknown-map";
			if (writer != null && world.WorldTick >= lastObservedTick && worldMapUid == mapUid)
				return true;

			writer?.Dispose();
			Directory.CreateDirectory(outputDirectory);

			episodeId = $"{DateTime.UtcNow:yyyy-MM-ddTHHmmssfffZ}-{Guid.NewGuid():N}";
			mapUid = worldMapUid;
			lastObservedTick = -1;

			var outputPath = Path.Combine(outputDirectory, $"{episodeId}.jsonl");
			writer = new StreamWriter(new FileStream(outputPath, FileMode.Create, FileAccess.Write, FileShare.Read));

			WriteRecord(new
			{
				schema_version = SchemaVersion,
				record_type = "episode_start",
				episode_id = episodeId,
				map_uid = mapUid,
				tick = world.WorldTick,
				sample_every_ticks = sampleEveryTicks
			});

			return true;
		}

		void WriteRecord(object record)
		{
			if (writer == null)
				return;

			writer.WriteLine(JsonSerializer.Serialize(record));
			writer.Flush();
		}

		void GatherWorldData(World world)
		{
			tick = world.WorldTick;
			players = world.Players;
			actorList = world.Actors.ToList();
			map = world.Map;
		}

		PlayerData GatherPlayerData(Player player)
		{
			return new PlayerData
			{
				Name = player.PlayerName,
				Faction = player.Faction?.Name ?? "Unknown",
				Units = actorList
					.Where(a => a.Owner == player && !a.Info.Name.Contains("building"))
					.Select(GatherActorData)
					.ToList(),
				Buildings = actorList
					.Where(a => a.Owner == player && a.Info.Name.Contains("building"))
					.Select(GatherActorData)
					.ToList()
			};
		}

		ActorData GatherActorData(Actor actor)
		{
			return new ActorData
			{
				Id = actor.ActorID,
				Type = actor.Info.Name,
				Position = actor.Location,
				Health = actor.TraitOrDefault<IHealth>()?.HP ?? 0,
				IsDead = actor.IsDead,
				Owner = actor.Owner?.PlayerName ?? "Neutral",
				CurrentAction = actor.CurrentActivity?.GetType().Name ?? "Idle"
			};
		}

		public string ExportGameState(World world)
		{
			GatherWorldData(world);

			playerDataList = players.Select(GatherPlayerData).ToList();

			var exportObj = new
			{
				Tick = tick,
				Players = playerDataList,
				Map = new
				{
					Size = map?.MapSize,
					Resources = map?.Resources
				}
			};

			return JsonSerializer.Serialize(exportObj);
		}

		public void RecordTick(World world)
		{
			if (!EnsureWriter(world))
				return;

			if (world.WorldTick % sampleEveryTicks != 0)
				return;

			var actors = world.Actors.ToList();
			var playersSummary = world.Players.Where(player => player.Playable && !player.NonCombatant).Select(player => new
			{
				player_id = player.PlayerActor?.ActorID ?? 0u,
				player_name = player.ResolvedPlayerName,
				internal_name = player.InternalName,
				client_index = player.ClientIndex,
				faction = player.Faction?.Name ?? "Unknown",
				actor_count = actors.Count(a => a.Owner == player && !a.IsDead),
				total_hp = actors.Where(a => a.Owner == player && !a.IsDead).Sum(a => a.TraitOrDefault<IHealth>()?.HP ?? 0)
			}).ToList();

			WriteRecord(new
			{
				schema_version = SchemaVersion,
				record_type = "tick",
				episode_id = episodeId,
				map_uid = mapUid,
				tick = world.WorldTick,
				observation = new
				{
					players = playersSummary
				}
			});

			lastObservedTick = world.WorldTick;
		}

		public void RecordRewardEvent(World world, Player player, string eventType, int rewardValue, object details)
		{
			if (!EnsureWriter(world))
				return;

			WriteRecord(new
			{
				schema_version = SchemaVersion,
				record_type = "reward",
				episode_id = episodeId,
				map_uid = mapUid,
				tick = world.WorldTick,
				player_id = player.PlayerActor?.ActorID ?? 0u,
				player_name = player.ResolvedPlayerName,
				event_type = eventType,
				reward_value = rewardValue,
				details
			});
		}

		public void RecordProcessedOrder(World world, int netFrame, int clientId, Order order)
		{
			if (order == null || !EnsureWriter(world))
				return;

			WriteRecord(new
			{
				schema_version = SchemaVersion,
				record_type = "action",
				episode_id = episodeId,
				map_uid = mapUid,
				tick = world.WorldTick,
				net_frame = netFrame,
				client_id = clientId,
				action = new
				{
					order_string = order.OrderString,
					subject_actor_id = order.Subject?.ActorID,
					queued = order.Queued,
					target_string = order.TargetString,
					extra_data = order.ExtraData,
					grouped_actor_ids = order.GroupedActors?
                    .Where(a => a != null)
                    .Select(a => a.ActorID)
                    .ToArray() ?? []
				}
			});
		}
	}
}
