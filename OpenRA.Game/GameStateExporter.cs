#pragma warning disable IDE0055
using System.Linq;
using System.Text.Json;
using OpenRA.Support;
using System.Collections.Generic;
using OpenRA.Traits;

namespace OpenRA.Game
{
    public sealed class GameStateExporter
    {
        int tick;
        private Player[] players = [];
        private List<Actor> actorList = [];
        private Map map;

        // Example per-player data structure
        private class PlayerData
        {
            public string Name { get; set; }
            public string Faction { get; set; }
            public List<ActorData> Units { get; set; } = [];
            public List<ActorData> Buildings { get; set; } = [];
        }

        // Example per-actor data structure
        private class ActorData
        {
            public uint Id { get; set; }
            public string Type { get; set; }
            public object Position { get; set; }
            public int Health { get; set; }
            public bool IsDead { get; set; }
            public string Owner { get; set; }
            public string CurrentAction { get; set; }
        }

        private List<PlayerData> playerDataList = [];

        private void gatherWorldData(World world)
        {
            tick = world.WorldTick;
            players = world.Players;
            actorList = world.Actors.ToList();
            map = world.Map;
        }

        private PlayerData gatherPlayerData(Player player)
        {
            var pdata = new PlayerData
            {
                Name = player.PlayerName,
                Faction = player.Faction?.Name ?? "Unknown"
            };

            // Units
            pdata.Units = actorList
                .Where(a => a.Owner == player && !a.Info.Name.Contains("building"))
                .Select(gatherActorData)
                .ToList();

            // Buildings
            pdata.Buildings = actorList
                .Where(a => a.Owner == player && a.Info.Name.Contains("building"))
                .Select(gatherActorData)
                .ToList();

            return pdata;
        }

        private ActorData gatherActorData(Actor actor)
        {
            return new ActorData
            {
                Id = actor.ActorID,
                Type = actor.Info.Name,
                Position = actor.Location, // You may want to format this
                Health = actor.TraitOrDefault<IHealth>()?.HP ?? 0,
                IsDead = actor.IsDead,
                Owner = actor.Owner?.PlayerName ?? "Neutral",
                CurrentAction = actor.CurrentActivity?.GetType().Name ?? "Idle"
            };
        }

        public string ExportGameState(World world)
        {
            gatherWorldData(world);

            playerDataList = players.Select(gatherPlayerData).ToList();

            var exportObj = new
            {
                Tick = tick,
                Players = playerDataList,
                Map = new
                {
                    Size = map?.MapSize,
                    Resources = map?.Resources
                    // Add more map info as needed
                }
            };

            return JsonSerializer.Serialize(exportObj);
        }
    }
}