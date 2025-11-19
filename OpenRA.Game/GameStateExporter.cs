#pragma warning disable IDE0055
using System.Linq;
using System.Text.Json;
using OpenRA.Support;
using System.Collections.Generic;

namespace OpenRA.Game
{
    public sealed class GameStateExporter
    {
        int tick;
        private Player[] Players = [];
        private List<Actor> actorList = [];

        private Map map;

        private void gatherWorldData(World world)
        {
            tick = world.WorldTick;
            Players = world.Players;
            actorList = world.Actors.ToList();
            map = world.Map;
        }

        string playerName;
        string faction;


        private void gatherPlayerData(Player player)
        {
            playerName = player.PlayerName;
            faction = player.Faction.Name;
        }



    }
}