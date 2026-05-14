"""``/recent`` slash command (issue #96).

Renders the per-guild ring of recently-played tracks newest-first.
Read-only — no voice precondition.
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import track_history, ux
from ..i18n import locale_for_ephemeral, locale_for_public, t

loader = lightbulb.Loader()


@loader.command
class Recent(
    lightbulb.SlashCommand,
    name="recent",
    description=t("recent.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_recent(ctx)


async def _handle_recent(ctx: lightbulb.Context) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond(
            t("voice.error.run_in_server", locale=locale_for_ephemeral(ctx), command="recent"),
            ephemeral=True,
        )
        return

    history = track_history.get(guild_id)
    if not history:
        await ctx.respond(
            t("recent.error.no_history", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    embed = ux.build_recent_embed(history, locale=locale_for_public(ctx))
    await ctx.respond(embed=embed)
