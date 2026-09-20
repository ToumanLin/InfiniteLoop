using AscNet.Common.MsgPack;
using AscNet.Common.Util;
using AscNet.Table.V2.share.photomode;

namespace AscNet.GameServer.Commands
{
    [CommandName("scene")]
    internal class SceneCommand : Command
    {
        public SceneCommand(Session session, string[] args, bool validate = true) : base(session, args, validate) { }

        public override string Help => "Unlock every home scene background with 'all', or unlock/select one catalog background by id.";

        [Argument(0, @"^unlock$|^set$", "The operation selected (unlock or set)")]
        string Op { get; set; } = string.Empty;

        [Argument(1, @"^all$|^[0-9]+$", "'all' for unlock, or a catalog scene background id")]
        string Target { get; set; } = string.Empty;

        public override void Execute()
        {
            if (Op is not ("unlock" or "set"))
                throw new InvalidOperationException("Invalid operation!");

            if (Target == "all")
            {
                if (Op != "unlock")
                    throw new CommandMessageCallbackException("'all' is only supported by '/scene unlock'.");
                UnlockAll();
                return;
            }

            if (!int.TryParse(Target, out int targetId) || !IsCatalogBackground(targetId))
                throw new CommandMessageCallbackException($"Background {Target} is not a valid scene background.");

            if (Op == "unlock")
                UnlockOne(targetId);
            else
                SetActive(targetId);
        }

        private static bool IsCatalogBackground(int id)
            => TableReaderV2.Parse<BackgroundTable>()
                .Any(background => background.Id == id && background.SceneModelId > 0);

        private void UnlockAll()
        {
            List<int> catalogIds = TableReaderV2.Parse<BackgroundTable>()
                .Where(background => background.Id > 0 && background.SceneModelId > 0)
                .Select(background => background.Id)
                .Distinct()
                .Order()
                .ToList();

            List<int> owned = session.player.OwnedBackgroundIds ?? new List<int>();
            List<int> added = catalogIds.Where(id => !owned.Contains(id)).ToList();

            if (added.Count == 0)
                throw new CommandMessageCallbackException("All scene backgrounds are already unlocked.");

            List<int> original = owned.ToList();
            session.player.OwnedBackgroundIds = owned.Union(catalogIds).Distinct().Order().ToList();

            try
            {
                session.player.SaveChecked();
            }
            catch
            {
                session.player.OwnedBackgroundIds = original;
                throw new CommandMessageCallbackException("Failed to persist scene unlocks.");
            }

            foreach (int id in added)
                session.SendPush(new NotifyAddBackground { BackgroundId = id });

            throw new CommandMessageCallbackException($"Unlocked {added.Count} scene background(s).");
        }

        private void UnlockOne(int targetId)
        {
            List<int> owned = session.player.OwnedBackgroundIds ?? new List<int>();
            if (owned.Contains(targetId))
                throw new CommandMessageCallbackException($"Scene background {targetId} is already unlocked.");

            List<int> original = owned.ToList();
            session.player.OwnedBackgroundIds = owned.Append(targetId).Distinct().Order().ToList();

            try
            {
                session.player.SaveChecked();
            }
            catch
            {
                session.player.OwnedBackgroundIds = original;
                throw new CommandMessageCallbackException("Failed to persist scene unlock.");
            }

            session.SendPush(new NotifyAddBackground { BackgroundId = targetId });
            throw new CommandMessageCallbackException($"Unlocked scene background {targetId}.");
        }

        private void SetActive(int targetId)
        {
            List<int> owned = session.player.OwnedBackgroundIds ?? new List<int>();
            bool grant = !owned.Contains(targetId);
            if (!grant && session.player.UseBackgroundId == targetId)
                throw new CommandMessageCallbackException($"Scene background {targetId} is already active.");

            List<int> original = owned.ToList();
            int originalUseBackgroundId = session.player.UseBackgroundId;
            if (grant)
                session.player.OwnedBackgroundIds = owned.Append(targetId).Distinct().Order().ToList();
            session.player.UseBackgroundId = targetId;

            try
            {
                session.player.SaveChecked();
            }
            catch
            {
                session.player.OwnedBackgroundIds = original;
                session.player.UseBackgroundId = originalUseBackgroundId;
                throw new CommandMessageCallbackException("Failed to persist scene selection.");
            }

            if (grant)
                session.SendPush(new NotifyAddBackground { BackgroundId = targetId });

            throw new CommandMessageCallbackException(grant
                ? $"Unlocked and set active background to {targetId}."
                : $"Set active background to {targetId}.");
        }
    }
}
