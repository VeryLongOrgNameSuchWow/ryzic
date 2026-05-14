"""``/replay`` slash command (issue #96).

Re-queues a previously-played track by routing through ``_handle_play``
so the cache/pinning/voice-handshake logic stays in one place. Position
is 1-indexed and defaults to 1 (most recent).
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import track_history
from ..i18n import locale_for_ephemeral, t
from .play import _handle_play

loader = lightbulb.Loader()


@loader.command
class Replay(
    lightbulb.SlashCommand,
    name="replay",
    description=t("replay.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    position = lightbulb.integer(
        "position",
        t("replay.param.position.description", locale="en_US"),
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
        await ctx.respond(
            t("voice.error.run_in_server", locale=locale_for_ephemeral(ctx), command="replay"),
            ephemeral=True,
        )
        return

    history = track_history.get(guild_id)
    if not history:
        # Shares ``recent.error.no_history`` — same copy, same domain
        # (track-history empty), no per-command divergence in v2 plan.
        await ctx.respond(
            t("recent.error.no_history", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    # ``min_value=1`` already constrains position ≥ 1; the upper bound
    # is the static MAX_HISTORY_SIZE cap. The actual ring may be
    # shorter (boot-fresh guild), so we still bounds-check against the
    # live length here.
    if position > len(history):
        await ctx.respond(
            t("replay.error.out_of_range", locale=locale_for_ephemeral(ctx), count=len(history)),
            ephemeral=True,
        )
        return

    track = history[position - 1]
    # Defer here (not inside ``_handle_play``) so /play's own ``Play.invoke``
    # remains responsible for its defer call — ``_handle_play`` is the
    # shared body, callers own their own response lifecycle.
    await ctx.defer()
    await _handle_play(ctx, track.url)
