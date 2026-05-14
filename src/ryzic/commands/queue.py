"""``/queue`` slash command (M1 §3, paging #99 + ``private`` flag #100).

Renders the now-playing track plus the upcoming queue. Two optional
arguments:

- ``page`` (issue #99) slices into the queue at offset
  ``(page-1) * QUEUE_PAGE_SIZE``; default 1 (the next-up tracks).
- ``private`` (issue #100) routes the success embed to an ephemeral
  response so the invoker can check the queue without spamming a busy
  text channel. Empty/error paths are ephemeral regardless — failure
  responses don't pollute the channel either way.

No voice precondition — queue introspection is read-only and useful
from any text channel in the guild.
"""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue, ux
from ..i18n import t

loader = lightbulb.Loader()


@loader.command
class Queue(
    lightbulb.SlashCommand,
    name="queue",
    description="Show the current track and upcoming queue.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    page = lightbulb.integer(
        "page",
        "Page number (10 tracks per page; 1 = next up).",
        default=1,
        min_value=1,
    )
    private = lightbulb.boolean(
        "private",
        "Only you see the response (default: False — public).",
        default=False,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_queue(ctx, page=self.page, private=self.private)


async def _handle_queue(ctx: lightbulb.Context, *, page: int = 1, private: bool = False) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond(
            t("voice.error.run_in_server", locale="en_US", command="queue"),
            ephemeral=True,
        )
        return

    empty_message = "Queue is empty and nothing is playing."

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond(empty_message, ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or player.current is None:
        await ctx.respond(empty_message, ephemeral=True)
        return

    now_playing_info = ux.get_track_info(player.current)
    if now_playing_info is None:
        # Defensive: a track entered the queue without metadata
        # (e.g. a future code path that bypasses ``attach_track_info``).
        # Treat as if nothing is playing rather than rendering a half-
        # populated embed.
        await ctx.respond(empty_message, ephemeral=True)
        return

    queue_entries: list[tuple[ux.TrackInfo, int]] = []
    for queued in player.queue:
        info = ux.get_track_info(queued)
        if info is None:
            continue
        queue_entries.append((info, int(queued.requester)))

    # ``min_value=1`` already enforces page ≥ 1 at the slash layer; we
    # only need to bound the upper end here. ``max(1, ...)`` keeps the
    # empty-queue case (0 entries → 0 pages naturally) from producing a
    # nonsense ``page > 0`` ephemeral when the user passes the default.
    total_pages = max(1, (len(queue_entries) + ux.QUEUE_PAGE_SIZE - 1) // ux.QUEUE_PAGE_SIZE)
    if page > total_pages:
        plural = "" if total_pages == 1 else "s"
        await ctx.respond(
            f"Queue has only {total_pages} page{plural}.",
            ephemeral=True,
        )
        return

    embed = ux.build_queue_embed(
        now_playing=now_playing_info,
        now_playing_position_ms=player.position,
        paused=player.paused,
        queue=queue_entries,
        page=page,
        total_pages=total_pages,
        locale="en_US",
    )
    await ctx.respond(embed=embed, ephemeral=private)
