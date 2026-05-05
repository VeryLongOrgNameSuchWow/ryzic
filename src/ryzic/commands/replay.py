"""``/replay`` slash command (issue #96).

Re-queues a previously-played track by routing through ``_handle_play``
so the cache/pinning/voice-handshake logic stays in one place. Position
is 1-indexed and defaults to 1 (most recent).
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import track_history
from .play import _handle_play

loader = lightbulb.Loader()


@loader.command
class Replay(
    lightbulb.SlashCommand,
    name="replay",
    description="Re-queue a previously-played track from /recent.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    position = lightbulb.integer(
        "position",
        "Position in /recent (1 = most recent). Defaults to 1.",
        default=1,
        min_value=1,
        max_value=track_history.MAX_HISTORY_SIZE,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_replay(ctx, self.position)


async def _handle_replay(ctx: lightbulb.Context, position: int) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond("Run /replay in a server.", ephemeral=True)
        return

    history = track_history.get(guild_id)
    if not history:
        await ctx.respond("No tracks have played yet.", ephemeral=True)
        return

    # ``min_value=1`` already constrains position ≥ 1; the upper bound
    # is the static MAX_HISTORY_SIZE cap. The actual ring may be
    # shorter (boot-fresh guild), so we still bounds-check against the
    # live length here.
    if position > len(history):
        await ctx.respond(
            f"Only {len(history)} track(s) in history.",
            ephemeral=True,
        )
        return

    track = history[position - 1]
    # Defer here (not inside ``_handle_play``) so /play's own ``Play.invoke``
    # remains responsible for its defer call — ``_handle_play`` is the
    # shared body, callers own their own response lifecycle.
    await ctx.defer()
    await _handle_play(ctx, track.url)
