"""``/resume`` slash command (M1 §3)."""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue, now_playing
from ..i18n import t
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Resume(
    lightbulb.SlashCommand,
    name="resume",
    description="Resume the paused track.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_resume(ctx)


async def _handle_resume(ctx: lightbulb.Context) -> None:
    if await ensure_same_voice(ctx) is None:
        return

    guild_id = cast(int, ctx.guild_id)

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond(t("np.error.nothing_playing", locale="en_US"), ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or not player.is_playing:
        await ctx.respond(t("np.error.nothing_playing", locale="en_US"), ephemeral=True)
        return

    if not player.paused:
        await ctx.respond("Already playing. Use /pause to pause.", ephemeral=True)
        return

    await player.set_pause(False)
    await ctx.respond("Resumed.")
    await now_playing.refresh(cast(hikari.GatewayBot, ctx.client.app), guild_id)
