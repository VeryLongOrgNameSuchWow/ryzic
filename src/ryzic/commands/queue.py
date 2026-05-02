"""``/queue`` slash command (M1 §3).

Renders the now-playing track plus the upcoming queue. No voice
precondition — queue introspection is read-only and useful from any
text channel in the guild.
"""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue, ux

loader = lightbulb.Loader()


@loader.command
class Queue(
    lightbulb.SlashCommand,
    name="queue",
    description="Show the current track and upcoming queue.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_queue(ctx)


async def _handle_queue(ctx: lightbulb.Context) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond("Run /queue in a server.", ephemeral=True)
        return

    empty_message = "Queue is empty and nothing is playing."

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond(empty_message, ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or player.current is None:
        await ctx.respond(empty_message, ephemeral=True)
        return

    now_playing_info = ux.get_track_info(player.current)
    if now_playing_info is None:
        # Defensive: a track entered the queue without metadata
        # (e.g. a future code path that bypasses ``attach_track_info``).
        # Treat as if nothing is playing rather than rendering a half-
        # populated embed.
        await ctx.respond(empty_message, ephemeral=True)
        return

    queue_entries: list[tuple[ux.TrackInfo, int]] = []
    for queued in player.queue:
        info = ux.get_track_info(queued)
        if info is None:
            continue
        queue_entries.append((info, int(queued.requester)))

    embed = ux.build_queue_embed(
        now_playing=now_playing_info,
        now_playing_position_ms=player.position,
        paused=player.paused,
        queue=queue_entries,
    )
    await ctx.respond(embed=embed)
