"""``/skip`` slash command (M1 §3).

Advances to the next track via :meth:`lavalink.DefaultPlayer.skip` —
deliberately not ``stop()`` then ``play()`` because the latter races
into Lavalink.py#153 (see ``lavalink_glue.py`` module docstring).

Per spec, skip-while-paused advances the queue but the new track stays
paused; the user must ``/resume``. ``DefaultPlayer.skip`` calls
``play()`` which intentionally leaves ``paused`` alone, so we don't
need to do anything special here — we only document the behaviour to
flag the surprising-but-spec'd outcome to readers.
"""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
import lightbulb

from .. import lavalink_glue, ux
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Skip(
    lightbulb.SlashCommand,
    name="skip",
    description="Skip the currently-playing track.",
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_skip(ctx)


async def _handle_skip(ctx: lightbulb.Context) -> None:
    """Skip the active track for ``ctx``'s guild after the voice precondition."""
    if await ensure_same_voice(ctx) is None:
        return

    # ``ensure_same_voice`` already verified guild_id is not None and
    # responded if so — narrowing for the type checker.
    guild_id = cast(int, ctx.guild_id)

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond("Nothing is playing.", ephemeral=True)
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
    if player is None or not player.is_playing or player.current is None:
        await ctx.respond("Nothing is playing.", ephemeral=True)
        return

    skipped_info = ux.get_track_info(player.current)
    skipped_title = skipped_info.title if skipped_info is not None else player.current.title

    await player.skip()

    safe_title = ux.safe_truncate(ux.escape_markdown(skipped_title), 256)
    message = f"Skipped **{safe_title}**."
    # ``DefaultPlayer.skip`` synchronously pops the next track off
    # ``queue`` and plays it; if the queue was empty it instead clears
    # ``current`` and dispatches QueueEndEvent. We append the "queue is
    # empty" suffix only when both are gone — otherwise a queue with
    # one track left would say "empty" while the just-promoted track
    # plays, which is misleading.
    if player.current is None and not player.queue:
        message = f"{message} Queue is empty."
    await ctx.respond(message)
