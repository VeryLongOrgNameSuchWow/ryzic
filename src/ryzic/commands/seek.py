"""``/seek`` slash command: jump to a position in the current track."""

from __future__ import annotations

from typing import cast

import hikari
import lightbulb

from .. import lavalink_glue
from ..i18n import locale_for_ephemeral, locale_for_public, t
from ..ux import format_duration, parse_seek_position
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Seek(
    lightbulb.SlashCommand,
    name="seek",
    description=t("seek.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    position = lightbulb.string(
        "position",
        t("seek.param.position.description", locale="en_US"),
        min_length=1,
        max_length=16,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_seek(ctx, self.position)


async def _handle_seek(ctx: lightbulb.Context, raw_position: str) -> None:
    if await ensure_same_voice(ctx) is None:
        return

    guild_id = cast(int, ctx.guild_id)

    player = lavalink_glue.get_player(guild_id)
    if player is None or player.current is None:
        await ctx.respond(
            t("np.error.nothing_playing", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    duration_ms = int(player.current.duration or 0)
    if duration_ms <= 0:
        await ctx.respond(
            t("seek.error.live_or_unknown", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    parsed = parse_seek_position(raw_position)
    if parsed is None:
        await ctx.respond(
            t("seek.error.bad_position", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    is_relative, value_ms = parsed
    target_ms = int(player.position) + value_ms if is_relative else value_ms
    target_ms = max(0, min(target_ms, duration_ms))

    await player.seek(target_ms)
    await ctx.respond(
        t(
            "seek.success.jumped",
            locale=locale_for_public(ctx),
            position=format_duration(target_ms),
        )
    )
