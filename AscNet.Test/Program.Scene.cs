using AscNet.Common.MsgPack;
using AscNet.Common.Util;
using AscNet.GameServer;
using AscNet.GameServer.Commands;
using AscNet.GameServer.Handlers;
using MongoDB.Bson;
using MongoDB.Bson.Serialization;
using System.Reflection;
using Inventory = AscNet.Common.Database.Inventory;

namespace AscNet.Test
{
    internal partial class Program
    {
        private static void ValidateSceneCommandCompatibility()
        {
            using MongoCollectionOverride noOpStages = MongoCollectionOverride.InstallNoOpStageCollection(); // login persists Stage rollover
            List<int> catalogIds = TableReaderV2.Parse<AscNet.Table.V2.share.photomode.BackgroundTable>()
                .Where(background => background.Id > 0 && background.SceneModelId > 0)
                .Select(background => background.Id)
                .Distinct()
                .Order()
                .ToList();
            AssertEqual(17, catalogIds.Count, "current home-scene catalog gap count derives from Background table");

            using MongoCollectionOverride mongoOverride = MongoCollectionOverride.InstallForDailySignInCompatibility(
                out RecordingMongoCollectionProxy<AscNet.Common.Database.Player> playerCollection,
                out _,
                out _);

            AscNet.GameServer.Commands.CommandFactory.commands.Clear();
            AscNet.GameServer.Commands.CommandFactory.LoadCommands();
            Type sceneCommandType = AscNet.GameServer.Commands.CommandFactory.commands.GetValueOrDefault("scene")
                ?? throw new InvalidDataException("CommandFactory.LoadCommands: expected command 'scene' to be discoverable.");
            AssertEqual("AscNet.GameServer.Commands.SceneCommand", sceneCommandType.FullName, "CommandFactory.LoadCommands command 'scene' type");

            const long playerId = 99_501;
            const int selectedBackgroundId = 14000005;
            AscNet.Common.Database.Player player = CreateDrawCompatibilityPlayer(playerId);
            player.UseBackgroundId = selectedBackgroundId;
            player.OwnedBackgroundIds = [];

            using LoopbackSessionHarness harness = new(
                CreateDrawCompatibilityCharacter(playerId),
                player,
                CreateDrawCompatibilityInventory(playerId, []),
                "scene-command-compat-test");

            void AssertNoExtraScenePacket(LoopbackSessionHarness h, string name)
            {
                if (h.TryReadAvailablePacket($"{name} unexpected packet", out Packet extra))
                    throw new InvalidDataException($"{name}: unexpected extra {extra.Type} packet.");
            }

            AscNet.GameServer.Commands.Command unlock = AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                "scene", harness.Session, ["unlock", "all"])
                ?? throw new InvalidDataException("SceneCommand: expected CommandFactory to create the command.");
            AssertEqual("Unlock every home scene background with 'all', or unlock/select one catalog background by id.", unlock.Help, "SceneCommand Help");

            string? completionMessage = null;
            try { unlock.Execute(); }
            catch (CommandMessageCallbackException ex) { completionMessage = ex.Message; }
            AssertEqual($"Unlocked {catalogIds.Count} scene background(s).", completionMessage, "SceneCommand completion feedback");

            AssertEqual(string.Join(",", catalogIds), string.Join(",", player.OwnedBackgroundIds), "SceneCommand persisted owned background ids");
            AssertEqual(selectedBackgroundId, player.UseBackgroundId, "SceneCommand preserves selected background");

            HashSet<int> pushed = new();
            for (int i = 0; i < catalogIds.Count; i++)
            {
                NotifyAddBackground add = ReadPushPayload<NotifyAddBackground>(
                    harness, nameof(NotifyAddBackground), $"SceneCommand NotifyAddBackground push {i}");
                pushed.Add(add.BackgroundId);
            }
            AssertEqual(string.Join(",", catalogIds), string.Join(",", pushed.Order()), "SceneCommand NotifyAddBackground pushes cover every catalog id");
            AssertNoExtraScenePacket(harness, "scene unlock");

            AssertEqual(1, playerCollection.ReplaceOneCalls, "SceneCommand persists exactly once");

            AscNet.Common.Database.Player relogged = BsonSerializer.Deserialize<AscNet.Common.Database.Player>(
                playerCollection.LastReplacement.ToBson());
            AssertEqual(string.Join(",", catalogIds), string.Join(",", relogged.OwnedBackgroundIds), "SceneCommand owned ids survive BSON relog");

            MethodInfo buildNotifyLogin = RequiredMethod(
                RequiredAscNetGameServerType("AscNet.GameServer.Handlers.AccountModule"),
                "BuildNotifyLogin",
                BindingFlags.Static | BindingFlags.NonPublic,
                [typeof(Session)]);
            NotifyLogin login = buildNotifyLogin.Invoke(null, [harness.Session]) as NotifyLogin
                ?? throw new InvalidDataException("AccountModule.BuildNotifyLogin returned nil.");
            AssertEqual(
                string.Join(",", catalogIds),
                string.Join(",", login.HaveBackgroundIds.Select(id => (int)id).Order()),
                "NotifyLogin HaveBackgroundIds reflect unlocked ownership");

            int savesBeforeRepeat = playerCollection.ReplaceOneCalls;
            string? repeatMessage = null;
            AscNet.GameServer.Commands.Command repeat = AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                "scene", harness.Session, ["unlock", "all"])
                ?? throw new InvalidDataException("SceneCommand repeat: expected CommandFactory to create the command.");
            try { repeat.Execute(); }
            catch (CommandMessageCallbackException ex) { repeatMessage = ex.Message; }
            AssertEqual("All scene backgrounds are already unlocked.", repeatMessage, "SceneCommand repeat no-op feedback");
            AssertEqual(savesBeforeRepeat, playerCollection.ReplaceOneCalls, "SceneCommand repeat does not save");
            AssertNoExtraScenePacket(harness, "scene repeat");

            AscNet.Common.Database.Player failingPlayer = CreateDrawCompatibilityPlayer(playerId + 1);
            failingPlayer.UseBackgroundId = selectedBackgroundId;
            failingPlayer.OwnedBackgroundIds = [catalogIds[0]];
            using LoopbackSessionHarness failingHarness = new(
                CreateDrawCompatibilityCharacter(playerId + 1),
                failingPlayer,
                CreateDrawCompatibilityInventory(playerId + 1, []),
                "scene-command-failure-test");
            playerCollection.ThrowOnReplaceOne = true;
            string? failureMessage = null;
            AscNet.GameServer.Commands.Command failing = AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                "scene", failingHarness.Session, ["unlock", "all"])
                ?? throw new InvalidDataException("SceneCommand failure: expected CommandFactory to create the command.");
            try { failing.Execute(); }
            catch (CommandMessageCallbackException ex) { failureMessage = ex.Message; }
            AssertEqual("Failed to persist scene unlocks.", failureMessage, "SceneCommand persistence failure feedback");
            AssertEqual(
                catalogIds[0].ToString(),
                string.Join(",", failingPlayer.OwnedBackgroundIds),
                "SceneCommand persistence failure rolls ownership back");

            playerCollection.ThrowOnReplaceOne = false;

            const long focusedPlayerId = 99_510;
            const int freeBackgroundId = 14000001;
            const int targetBackgroundId = 14000011;
            const int invalidBackgroundId = 99999999;
            int otherBackgroundId = catalogIds.First(id => id != freeBackgroundId && id != targetBackgroundId);
            AscNet.Common.Database.Player focusedPlayer = CreateDrawCompatibilityPlayer(focusedPlayerId);
            focusedPlayer.UseBackgroundId = freeBackgroundId;
            focusedPlayer.OwnedBackgroundIds = [];
            using LoopbackSessionHarness focusedHarness = new(
                CreateDrawCompatibilityCharacter(focusedPlayerId),
                focusedPlayer,
                CreateDrawCompatibilityInventory(focusedPlayerId, []),
                "scene-command-focused-test");

            int sceneSaves = playerCollection.ReplaceOneCalls;

            string? invalidUnlockMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["unlock", invalidBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { invalidUnlockMessage = ex.Message; }
            AssertEqual($"Background {invalidBackgroundId} is not a valid scene background.", invalidUnlockMessage,
                "SceneCommand unlock noncatalog feedback");
            AssertEqual(0, focusedPlayer.OwnedBackgroundIds.Count,
                "SceneCommand unlock noncatalog leaves ownership empty");
            AssertEqual(freeBackgroundId, focusedPlayer.UseBackgroundId,
                "SceneCommand unlock noncatalog preserves active background");
            AssertEqual(sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand unlock noncatalog does not persist");
            AssertNoAvailablePacket(focusedHarness, "scene unlock noncatalog");

            string? unlockMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["unlock", targetBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { unlockMessage = ex.Message; }
            AssertEqual($"Unlocked scene background {targetBackgroundId}.", unlockMessage,
                "SceneCommand unlock id feedback");
            AssertEqual(targetBackgroundId.ToString(), string.Join(",", focusedPlayer.OwnedBackgroundIds),
                "SceneCommand unlock id ownership");
            AssertEqual(freeBackgroundId, focusedPlayer.UseBackgroundId,
                "SceneCommand unlock id preserves active background");
            AssertEqual(++sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand unlock id persists once");
            NotifyAddBackground unlockPush = ReadPushPayload<NotifyAddBackground>(
                focusedHarness, nameof(NotifyAddBackground), "SceneCommand unlock id push");
            AssertEqual(targetBackgroundId, unlockPush.BackgroundId, "SceneCommand unlock id push id");
            AssertNoAvailablePacket(focusedHarness, "scene unlock id");

            string? repeatUnlockMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["unlock", targetBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { repeatUnlockMessage = ex.Message; }
            AssertEqual($"Scene background {targetBackgroundId} is already unlocked.", repeatUnlockMessage,
                "SceneCommand repeat unlock id feedback");
            AssertEqual(sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand repeat unlock id does not persist");
            AssertNoAvailablePacket(focusedHarness, "scene repeat unlock id");

            string? setOwnedMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["set", targetBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { setOwnedMessage = ex.Message; }
            AssertEqual($"Set active background to {targetBackgroundId}.", setOwnedMessage,
                "SceneCommand set owned feedback");
            AssertEqual(targetBackgroundId, focusedPlayer.UseBackgroundId,
                "SceneCommand set updates active background");
            AssertEqual(targetBackgroundId.ToString(), string.Join(",", focusedPlayer.OwnedBackgroundIds),
                "SceneCommand set owned does not grant again");
            AssertEqual(++sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand set owned persists once");
            AssertNoAvailablePacket(focusedHarness, "scene set owned");

            string? setSameMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["set", targetBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { setSameMessage = ex.Message; }
            AssertEqual($"Scene background {targetBackgroundId} is already active.", setSameMessage,
                "SceneCommand set active feedback");
            AssertEqual(sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand set active does not persist");
            AssertNoAvailablePacket(focusedHarness, "scene set active");

            string? setGrantMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["set", otherBackgroundId.ToString()])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { setGrantMessage = ex.Message; }
            AssertEqual($"Unlocked and set active background to {otherBackgroundId}.", setGrantMessage,
                "SceneCommand set unowned feedback");
            AssertEqual(otherBackgroundId, focusedPlayer.UseBackgroundId,
                "SceneCommand set unowned updates active background");
            AssertEqual(
                string.Join(",", new[] { otherBackgroundId, targetBackgroundId }.Order()),
                string.Join(",", focusedPlayer.OwnedBackgroundIds),
                "SceneCommand set unowned grants ownership");
            AssertEqual(++sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand set unowned persists once");
            NotifyAddBackground setPush = ReadPushPayload<NotifyAddBackground>(
                focusedHarness, nameof(NotifyAddBackground), "SceneCommand set unowned push");
            AssertEqual(otherBackgroundId, setPush.BackgroundId, "SceneCommand set unowned push id");
            AssertNoAvailablePacket(focusedHarness, "scene set unowned");

            string? setAllMessage = null;
            try
            {
                AscNet.GameServer.Commands.CommandFactory.CreateCommand(
                        "scene", focusedHarness.Session, ["set", "all"])
                    ?.Execute();
            }
            catch (CommandMessageCallbackException ex) { setAllMessage = ex.Message; }
            AssertEqual("'all' is only supported by '/scene unlock'.", setAllMessage,
                "SceneCommand set all feedback");
            AssertEqual(otherBackgroundId, focusedPlayer.UseBackgroundId,
                "SceneCommand set all preserves active background");
            AssertEqual(sceneSaves, playerCollection.ReplaceOneCalls,
                "SceneCommand set all does not persist");
            AssertNoAvailablePacket(focusedHarness, "scene set all");

            AscNet.Common.Database.Player reloggedFocused = BsonSerializer.Deserialize<AscNet.Common.Database.Player>(
                playerCollection.LastReplacement.ToBson());
            AssertEqual(otherBackgroundId, reloggedFocused.UseBackgroundId,
                "SceneCommand active background survives BSON relog");
            AssertEqual(
                string.Join(",", focusedPlayer.OwnedBackgroundIds),
                string.Join(",", reloggedFocused.OwnedBackgroundIds),
                "SceneCommand owned ids survive BSON relog");

            HashSet<int> freeBackgroundIds = TableReaderV2.Parse<AscNet.Table.V2.share.photomode.BackgroundTable>()
                .Where(background => background.Id > 0 && background.IsFree > 0)
                .Select(background => background.Id)
                .ToHashSet();
            int unownedBackgroundId = catalogIds.First(id =>
                !freeBackgroundIds.Contains(id)
                && !focusedPlayer.OwnedBackgroundIds.Contains(id)
                && id != focusedPlayer.UseBackgroundId);
            const int displayCharId = 1021001;
            int displaySaves = playerCollection.ReplaceOneCalls;

            InvokeRegisteredRequestHandler(
                nameof(ChangeDisplayRequest),
                focusedHarness.Session,
                99_520,
                new ChangeDisplayRequest { BackgroundId = invalidBackgroundId, CharId = displayCharId, FashionId = 0 });
            ChangeDisplayResponse invalidDisplay = ReadResponsePayload<ChangeDisplayResponse>(
                focusedHarness, 99_520, nameof(ChangeDisplayResponse), "ChangeDisplay noncatalog response");
            AssertEqual(1, invalidDisplay.Code, "ChangeDisplay noncatalog Code");
            AssertEqual(otherBackgroundId, focusedPlayer.UseBackgroundId,
                "ChangeDisplay noncatalog preserves active background");
            AssertEqual(displaySaves, playerCollection.ReplaceOneCalls,
                "ChangeDisplay noncatalog does not persist");
            AssertNoAvailablePacket(focusedHarness, "ChangeDisplay noncatalog");

            InvokeRegisteredRequestHandler(
                nameof(ChangeDisplayRequest),
                focusedHarness.Session,
                99_521,
                new ChangeDisplayRequest { BackgroundId = unownedBackgroundId, CharId = displayCharId, FashionId = 0 });
            ChangeDisplayResponse unownedDisplay = ReadResponsePayload<ChangeDisplayResponse>(
                focusedHarness, 99_521, nameof(ChangeDisplayResponse), "ChangeDisplay unowned response");
            AssertEqual(1, unownedDisplay.Code, "ChangeDisplay unowned Code");
            AssertEqual(otherBackgroundId, focusedPlayer.UseBackgroundId,
                "ChangeDisplay unowned preserves active background");
            AssertEqual(0, focusedPlayer.PlayerData.DisplayCharIdList.Count,
                "ChangeDisplay unowned leaves display characters untouched");
            AssertEqual(displaySaves, playerCollection.ReplaceOneCalls,
                "ChangeDisplay unowned does not persist");
            AssertNoAvailablePacket(focusedHarness, "ChangeDisplay unowned");

            InvokeRegisteredRequestHandler(
                nameof(ChangeDisplayRequest),
                focusedHarness.Session,
                99_522,
                new ChangeDisplayRequest { BackgroundId = targetBackgroundId, CharId = displayCharId, FashionId = 0 });
            ChangeDisplayResponse ownedDisplay = ReadResponsePayload<ChangeDisplayResponse>(
                focusedHarness, 99_522, nameof(ChangeDisplayResponse), "ChangeDisplay owned response");
            AssertEqual(0, ownedDisplay.Code, "ChangeDisplay owned Code");
            AssertEqual(targetBackgroundId, focusedPlayer.UseBackgroundId,
                "ChangeDisplay owned updates active background");
            AssertEqual(++displaySaves, playerCollection.ReplaceOneCalls,
                "ChangeDisplay owned persists once");
            AssertNoAvailablePacket(focusedHarness, "ChangeDisplay owned");

            InvokeRegisteredRequestHandler(
                nameof(ChangeDisplayRequest),
                focusedHarness.Session,
                99_523,
                new ChangeDisplayRequest { BackgroundId = targetBackgroundId, CharId = displayCharId, FashionId = 0 });
            ChangeDisplayResponse repeatDisplay = ReadResponsePayload<ChangeDisplayResponse>(
                focusedHarness, 99_523, nameof(ChangeDisplayResponse), "ChangeDisplay repeat response");
            AssertEqual(0, repeatDisplay.Code, "ChangeDisplay repeat Code");
            AssertEqual(displaySaves, playerCollection.ReplaceOneCalls,
                "ChangeDisplay repeat does not persist");
            AssertNoAvailablePacket(focusedHarness, "ChangeDisplay repeat");

            AscNet.Common.Database.Player reloggedDisplay = BsonSerializer.Deserialize<AscNet.Common.Database.Player>(
                playerCollection.LastReplacement.ToBson());
            AssertEqual(targetBackgroundId, reloggedDisplay.UseBackgroundId,
                "ChangeDisplay active background survives BSON relog");
        }
    }
}
