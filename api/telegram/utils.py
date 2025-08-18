from telethon.tl.types import PeerChannel

from configs.config import Config


async def resolve_channel(client, cfg: Config):
    if cfg.channel_id:
        try:
            cid = int(cfg.channel_id)
            return await client.get_entity(PeerChannel(cid))
        except Exception:
            return await client.get_entity(int(cfg.channel_id))
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and dialog.name.strip().lower() == cfg.channel_title.strip().lower():
            return dialog.entity
    raise ValueError(
        "Channel not found. Provide CHANNEL_ID (preferred) or ensure CHANNEL_TITLE matches exactly."
    ) 