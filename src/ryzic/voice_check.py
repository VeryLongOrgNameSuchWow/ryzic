"""Voice-channel guard for commands that mutate playback (M1 §3 cross-cutting).

``/skip``, ``/pause``, ``/resume``, and ``/leave`` all share the same
prelude: bail unless the invoker is in the bot's voice channel.
:func:`ensure_same_voice` centralises the check so the rule is applied
identically and the failure message is consistent.

The function returns the bot's voice-channel id on success so the caller
doesn't have to re-derive it; ``None`` means "I already responded with
an ephemeral; abort the handler".
"""

from __future__ import annotations

from typing import cast

import hikari
import lightbulb


async def ensure_same_voice(ctx: lightbulb.Context) -> int | None:
    """Verify the invoker shares the bot's voice channel.

    Sends an ephemeral and returns ``None`` on failure (DM, bot not in
    voice, user not in voice, user in a different channel). Returns the
    bot's voice channel id on success.

    The lookup is cache-only (no REST round-trip): every voice state is
    pushed through the gateway, and ``GUILD_VOICE_STATES`` is in the
    intent set requested by ``bot.py``.
    """
    guild_id = ctx.guild_id
    if guild_id is None:
        # Defense in depth: ``dm_enabled=False`` at registration time
        # already blocks DMs, but a future re-registration mistake
        # shouldn't crash the handler.
        await ctx.respond("Run this in a server.", ephemeral=True)
        return None

    # ``Client.app`` is typed as ``RESTAware`` to support the REST-only
    # client path. We construct the ``GatewayEnabledClient`` flavour in
    # ``bot.py`` so the gateway-only attributes (``cache``, ``get_me``)
    # are always present at runtime; the cast is purely for ty.
    bot = cast(hikari.GatewayBot, ctx.client.app)
    me = bot.get_me()
    if me is None:
        await ctx.respond(
            "Bot is still starting up. Try again in a moment.",
            ephemeral=True,
        )
        return None

    bot_state = bot.cache.get_voice_state(guild_id, me.id)
    if bot_state is None or bot_state.channel_id is None:
        await ctx.respond("I'm not in a voice channel.", ephemeral=True)
        return None

    user_state = bot.cache.get_voice_state(guild_id, ctx.user.id)
    if user_state is None or user_state.channel_id != bot_state.channel_id:
        await ctx.respond(
            f"Join <#{bot_state.channel_id}> to use this.",
            ephemeral=True,
        )
        return None

    return int(bot_state.channel_id)
