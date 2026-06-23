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
import lavalink
import lightbulb

from .i18n import locale_for_ephemeral, t


async def ensure_same_voice(ctx: lightbulb.Context) -> int | None:
    """Verify the invoker shares the bot's voice channel.

    Sends an ephemeral and returns ``None`` on failure (DM, bot not in
    voice, user not in voice, user in a different channel). Returns the
    bot's voice channel id on success.

    The lookup is cache-only (no REST round-trip): every voice state is
    pushed through the gateway, and ``GUILD_VOICE_STATES`` is in the
    intent set requested by ``bot.py``.
    """
    locale = locale_for_ephemeral(ctx)
    guild_id = ctx.guild_id
    if guild_id is None:
        # Defense in depth: ``dm_enabled=False`` at registration time
        # already blocks DMs, but a future re-registration mistake
        # shouldn't crash the handler. The command name comes from
        # lightbulb's parsed command data so the message names the
        # specific slash command the user just ran (consolidates the
        # five per-command "Run /<cmd> in a server." sites onto one key).
        await ctx.respond(
            t("voice.error.run_in_server", locale=locale, command=ctx.command_data.name),
            ephemeral=True,
        )
        return None

    # ``Client.app`` is typed as ``RESTAware`` to support the REST-only
    # client path. We construct the ``GatewayEnabledClient`` flavour in
    # ``bot.py`` so the gateway-only attributes (``cache``, ``get_me``)
    # are always present at runtime; the cast is purely for ty.
    bot = cast(hikari.GatewayBot, ctx.client.app)
    me = bot.get_me()
    if me is None:
        await ctx.respond(t("voice.error.bot_starting", locale=locale), ephemeral=True)
        return None

    bot_state = bot.cache.get_voice_state(guild_id, me.id)
    if bot_state is None or bot_state.channel_id is None:
        await ctx.respond(t("voice.error.bot_not_in_voice", locale=locale), ephemeral=True)
        return None

    user_state = bot.cache.get_voice_state(guild_id, ctx.user.id)
    if user_state is None or user_state.channel_id != bot_state.channel_id:
        # ``<#%{channel_id}>`` renders as a Discord channel mention
        # client-side; no escape_markdown needed (it's not markdown).
        await ctx.respond(
            t(
                "voice.error.join_my_channel",
                locale=locale,
                channel_id=bot_state.channel_id,
            ),
            ephemeral=True,
        )
        return None

    return int(bot_state.channel_id)


async def check_player_or_respond(
    ctx: lightbulb.Context,
    player: lavalink.DefaultPlayer | None,
) -> lavalink.DefaultPlayer | None:
    """Three-way guard for ``/pause``, ``/resume``, ``/seek``, ``/skip``.

    Returns the player (caller proceeds) only when it is connected AND
    holds a current track. Responds ephemerally and returns ``None``
    (caller aborts) when there is no current track ("Nothing is
    playing.") or when a track is held but the player reports
    disconnected ("Reconnecting to voice …") — the transient resync
    window (region migration, voice-WS blip) where commanding the player
    would PATCH a server that considers it gone. Mirrors
    :func:`ensure_same_voice`'s respond-and-return convention. Replaces
    the duplicated ``if player is None or player.current is None`` block
    across the four command files.
    """
    locale = locale_for_ephemeral(ctx)
    if player is None or player.current is None:
        await ctx.respond(t("np.error.nothing_playing", locale=locale), ephemeral=True)
        return None
    if not player.is_connected:
        await ctx.respond(t("voice.error.reconnecting", locale=locale), ephemeral=True)
        return None
    return player
