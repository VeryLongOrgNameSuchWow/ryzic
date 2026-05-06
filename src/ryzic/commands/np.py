"""``/np`` slash command: show the currently-playing track.

Mirrors ``/queue``'s ``private`` flag (issue #100): ``private=True`` routes
the success embed to an ephemeral response so the invoker can check the
current track without spamming a busy channel. Empty/error paths stay
ephemeral regardless — failure messages don't pollute the channel
either way.
"""

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
    private = lightbulb.boolean(
        "private",
        "Only you see the response (default: False — public).",
        default=False,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_np(ctx, private=self.private)


async def _handle_np(ctx: lightbulb.Context, *, private: bool = False) -> None:
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

    embed = ux.build_simple_now_playing_embed(info, int(player.position), paused=player.paused)
    await ctx.respond(embed=embed, ephemeral=private)
