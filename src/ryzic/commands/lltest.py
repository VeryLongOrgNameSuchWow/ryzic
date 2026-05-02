"""Throwaway ``/lltest`` slash command — verifies the lavalink wire-up.

Removed by PR6b once ``/play`` (PR6a) exists and exercises the same path
end-to-end. Keeping this around alongside the real commands would just be
noise — its only purpose is to give PR5 something a maintainer can poke at
in a test guild before the real UX is built.
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import lavalink_glue

loader = lightbulb.Loader()


@loader.command
class LLTest(
    lightbulb.SlashCommand,
    name="lltest",
    description="Lavalink wire-up smoke check (removed once /play exists).",
    default_member_permissions=hikari.Permissions.MANAGE_GUILD,
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        ll_client = lavalink_glue.get_lavalink_client()
        if ll_client is None:
            await ctx.respond(
                "Lavalink is not ready yet. Wait a few seconds and try again.",
                ephemeral=True,
            )
            return

        if ctx.guild_id is None:
            await ctx.respond("Run this in a guild.", ephemeral=True)
            return

        nodes = list(ll_client.node_manager.nodes)
        if not nodes:
            await ctx.respond("No Lavalink nodes registered.", ephemeral=True)
            return

        lines = [
            f"- `{node.name}` region=`{node.region}` available=`{node.available}`" for node in nodes
        ]
        await ctx.respond(
            "Lavalink nodes:\n" + "\n".join(lines),
            ephemeral=True,
        )
