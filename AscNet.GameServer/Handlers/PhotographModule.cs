using AscNet.Common.MsgPack;
using AscNet.Common.Util;
using AscNet.Table.V2.share.fashion;
using AscNet.Table.V2.share.photomode;
using MessagePack;

namespace AscNet.GameServer.Handlers
{
    #region MsgPackScheme
#pragma warning disable CS8618 // Non-nullable field must contain a non-null value when exiting constructor. Consider declaring as nullable.
    [MessagePackObject(true)]
    public class ChangeDisplayRequest
    {
        public int FashionId;
        public int CharId;
        public int BackgroundId;
    }

    [MessagePackObject(true)]
    public class ChangeDisplayResponse
    {
        public int Code;
        public List<long> DisplayCharIdList;
    }
#pragma warning restore CS8618 // Non-nullable field must contain a non-null value when exiting constructor. Consider declaring as nullable.
    #endregion

    internal class PhotographModule
    {
        [RequestPacketHandler("ChangeDisplayRequest")]
        public static void ChangeDisplayRequestHandler(Session session, Packet.Request packet)
        {
            ChangeDisplayRequest request = packet.Deserialize<ChangeDisplayRequest>();

            bool validBackground = TableReaderV2.Parse<BackgroundTable>()
                .Any(background => background.Id == request.BackgroundId && background.SceneModelId > 0);
            if (!validBackground || !AccountModule.BuildHaveBackgroundIds(session.player).Contains(request.BackgroundId))
            {
                session.SendResponse(new ChangeDisplayResponse { Code = 1 }, packet.Id);
                return;
            }

            bool playerChanged = session.player.UseBackgroundId != request.BackgroundId;
            session.player.UseBackgroundId = request.BackgroundId;
            if (!session.player.PlayerData.DisplayCharIdList.Contains(request.CharId))
            {
                session.player.PlayerData.DisplayCharId = request.CharId;
                session.player.PlayerData.DisplayCharIdList.Add(request.CharId);
                playerChanged = true;
            }

            if (playerChanged)
                session.player.SaveChecked();

            CharacterData? character = session.character.Characters.Find(x => x.Id == request.CharId);
            if (character is not null && character.FashionId != request.FashionId && TableReaderV2.Parse<FashionTable>().Any(x => x.CharacterId == request.CharId && x.Id == request.FashionId))
            {
                character.FashionId = (uint)request.FashionId;

                NotifyCharacterDataList notifyCharacterData = new();
                notifyCharacterData.CharacterDataList.Add(character);

                session.SendPush(notifyCharacterData);
            }

            session.SendResponse(new ChangeDisplayResponse() { DisplayCharIdList = session.player.PlayerData.DisplayCharIdList }, packet.Id);
        }
    }
}
