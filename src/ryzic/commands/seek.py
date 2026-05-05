"""``/seek`` slash command: jump to a position in the current track."""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue
from ..ux import format_duration, parse_seek_position
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()

_BAD_POSITION = "Couldn't read that position. Use `m:ss` (e.g. `1:30`), `+SECONDS`, or `-SECONDS`."
_LIVE_OR_UNKNOWN = "Can't seek this track — its duration is unknown (livestream?)."


@loader.command
class Seek(
    lightbulb.SlashCommand,
    name="seek",
    description="Jump to a position in the current track (m:ss, +30, or -15).",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    position = lightbulb.string(
        "position",
        "Target position: `m:ss`, `+SECONDS` (forward), or `-SECONDS` (backward).",
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

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond("Nothing is playing.", ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or not player.is_playing or player.current is None:
        await ctx.respond("Nothing is playing.", ephemeral=True)
        return

    duration_ms = int(player.current.duration or 0)
    if duration_ms <= 0:
        await ctx.respond(_LIVE_OR_UNKNOWN, ephemeral=True)
        return

    parsed = parse_seek_position(raw_position)
    if parsed is None:
        await ctx.respond(_BAD_POSITION, ephemeral=True)
        return

    is_relative, value_ms = parsed
    target_ms = int(player.position) + value_ms if is_relative else value_ms
    target_ms = max(0, min(target_ms, duration_ms))

    await player.seek(target_ms)
    await ctx.respond(f"Jumped to {format_duration(target_ms)}.")
