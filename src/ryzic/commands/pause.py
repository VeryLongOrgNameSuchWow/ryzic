"""``/pause`` slash command (M1 §3)."""

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
class Pause(
    lightbulb.SlashCommand,
    name="pause",
    description="Pause the currently-playing track.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_pause(ctx)


async def _handle_pause(ctx: lightbulb.Context) -> None:
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

    if player.paused:
        await ctx.respond("Already paused. Use /resume.", ephemeral=True)
        return

    await player.set_pause(True)
    await ctx.respond("Paused.")
    await now_playing.refresh(cast(hikari.GatewayBot, ctx.client.app), guild_id)
