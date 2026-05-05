"""``/np`` slash command: show the currently-playing track."""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue, ux

loader = lightbulb.Loader()

_NOTHING_PLAYING = "Nothing is playing."


@loader.command
class NowPlaying(
    lightbulb.SlashCommand,
    name="np",
    description="Show the currently-playing track.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_np(ctx)


async def _handle_np(ctx: lightbulb.Context) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond("Run /np in a server.", ephemeral=True)
        return

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond(_NOTHING_PLAYING, ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or player.current is None:
        await ctx.respond(_NOTHING_PLAYING, ephemeral=True)
        return

    info = ux.get_track_info(player.current)
    if info is None:
        await ctx.respond(_NOTHING_PLAYING, ephemeral=True)
        return

    embed = ux.build_now_playing_embed(info, int(player.position), paused=player.paused)
    await ctx.respond(embed=embed)
