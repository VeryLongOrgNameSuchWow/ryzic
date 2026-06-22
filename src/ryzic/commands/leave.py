"""``/leave`` slash command (M1 §3).

Stops playback, clears the queue, and disconnects from voice. The
existing ``QueueEndEvent`` auto-leave timer is rendered moot by the
explicit disconnect; ``_on_voice_state_update`` in ``lavalink_glue``
clears ``_voice_ready_events[guild_id]`` once the gateway confirms our
own state went to ``channel_id is None``, so a follow-up ``/play``
correctly waits on a fresh handshake.
"""

from __future__ import annotations

from typing import cast

import hikari
import lightbulb

from .. import lavalink_glue, now_playing
from ..i18n import locale_for_ephemeral, locale_for_public, t
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Leave(
    lightbulb.SlashCommand,
    name="leave",
    description=t("leave.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_leave(ctx)


async def _handle_leave(ctx: lightbulb.Context) -> None:
    if await ensure_same_voice(ctx) is None:
        return

    guild_id = cast(int, ctx.guild_id)

    player = lavalink_glue.get_player(guild_id)
    if player is None or not player.is_connected:
        await ctx.respond(
            t("voice.error.bot_not_in_voice", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    # Stop first so Lavalink halts the stream cleanly; clearing the queue
    # before update_voice_state guarantees a follow-up auto-advance can
    # not race a disconnect. Use the releasing helper so audio_cache pins
    # for queued-but-never-played tracks don't leak (issue #24).
    await player.stop()
    await lavalink_glue.clear_queue_releasing(player)

    bot = cast(hikari.GatewayBot, ctx.client.app)
    # Mark as intentional so on_websocket_closed skips voice_lost broadcast.
    lavalink_glue._mark_intentional_disconnect(guild_id)
    try:
        await bot.update_voice_state(guild_id, None)
    except Exception:
        lavalink_glue._clear_intentional_disconnect(guild_id)
        raise

    await now_playing.teardown(bot, guild_id)
    await ctx.respond(t("leave.success.left", locale=locale_for_public(ctx)))
