async def resolve_channel(client, cfg):
    if cfg.channel_id:
        chan = (cfg.channel_id or "").strip()
        # If looks like username (starts with @ or contains letters), let Telethon resolve it directly
        if chan.startswith("@") or any(c.isalpha() for c in chan):
            return await client.get_entity(chan)
        # Otherwise try numeric id
        try:
            cid = int(chan)
            return await client.get_entity(cid)
        except Exception:
            # Fallback to plain get_entity with original string
            return await client.get_entity(chan)

    # Resolve by title as fallback
    title = (cfg.channel_title or "").strip().lower()
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and dialog.name and dialog.name.strip().lower() == title:
            return dialog.entity
    raise ValueError(
        "Channel not found. Provide CHANNEL_ID (preferred) or ensure CHANNEL_TITLE matches exactly."
    )

