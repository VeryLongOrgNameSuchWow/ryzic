"""``/recent`` slash command (issue #96).

Renders the per-guild ring of recently-played tracks newest-first.
Read-only — no voice precondition.
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import track_history, ux

loader = lightbulb.Loader()


@loader.command
class Recent(
    lightbulb.SlashCommand,
    name="recent",
    description="Show the last tracks played in this server.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_recent(ctx)


async def _handle_recent(ctx: lightbulb.Context) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond("Run /recent in a server.", ephemeral=True)
        return

    history = track_history.get(guild_id)
    if not history:
        await ctx.respond("No tracks have played yet.", ephemeral=True)
        return

    embed = ux.build_recent_embed(history, locale="en_US")
    await ctx.respond(embed=embed)
