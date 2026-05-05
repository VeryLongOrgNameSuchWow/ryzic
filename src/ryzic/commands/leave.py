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
import lavalink
import lightbulb

from .. import lavalink_glue, now_playing
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Leave(
    lightbulb.SlashCommand,
    name="leave",
    description="Disconnect from voice and clear the queue.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_leave(ctx)


async def _handle_leave(ctx: lightbulb.Context) -> None:
    if await ensure_same_voice(ctx) is None:
        return

    guild_id = cast(int, ctx.guild_id)

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond("I'm not in a voice channel.", ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or not player.is_connected:
        await ctx.respond("I'm not in a voice channel.", ephemeral=True)
        return

    # Stop first so Lavalink halts the stream cleanly; clearing the queue
    # before update_voice_state guarantees a follow-up auto-advance can
    # not race a disconnect. Use the releasing helper so audio_cache pins
    # for queued-but-never-played tracks don't leak (issue #24).
    await player.stop()
    await lavalink_glue.clear_queue_releasing(player)

    bot = cast(hikari.GatewayBot, ctx.client.app)
    await bot.update_voice_state(guild_id, None)

    await now_playing.teardown(bot, guild_id)
    await ctx.respond("Left voice channel. Queue cleared.")
