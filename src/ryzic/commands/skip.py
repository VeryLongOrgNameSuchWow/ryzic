"""``/skip`` slash command (M1 §3).

Advances to the next track via :meth:`lavalink.DefaultPlayer.skip` —
deliberately not ``stop()`` then ``play()`` because the latter races
into Lavalink.py#153 (see ``lavalink_glue.py`` module docstring).

Per spec, skip-while-paused advances the queue but the new track stays
paused; the user must ``/resume``. We explicitly re-pause after the
skip so the behaviour does not depend on Lavalink's track-replace
defaults: ``DefaultPlayer.play`` (which ``skip`` calls) sends an
``update_player`` body with no ``paused`` field, so whether the new
track inherits the prior paused state is undocumented server-side.
"""

from __future__ import annotations

from typing import cast

import hikari
import lightbulb

from .. import lavalink_glue, ux
from ..i18n import locale_for_ephemeral, locale_for_public, t
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Skip(
    lightbulb.SlashCommand,
    name="skip",
    description=t("skip.command.description", locale="en_US"),
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

    player = lavalink_glue.get_player(guild_id)
    if player is None or player.current is None:
        await ctx.respond(
            t("np.error.nothing_playing", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    skipped_info = ux.get_track_info(player.current)
    skipped_title = skipped_info.title if skipped_info is not None else player.current.title

    was_paused = player.paused
    await player.skip()
    # Defensive re-pause: ``DefaultPlayer.play`` does not include a
    # ``paused`` field on the track-replace, so server-side behaviour
    # is undocumented. Force the spec'd UX (skip-while-paused stays
    # paused; user must /resume) regardless of what the server picks.
    if was_paused and player.current is not None:
        await player.set_pause(True)

    # User-controlled title; escape before splicing into the catalog's bold template.
    safe_title = ux.safe_truncate(ux.escape_markdown(skipped_title), 256)
    # ``DefaultPlayer.skip`` synchronously pops the next track off
    # ``queue`` and plays it; if the queue was empty it instead clears
    # ``current`` and dispatches QueueEndEvent. We pick the "queue is
    # empty" variant only when both are gone — otherwise a queue with
    # one track left would say "empty" while the just-promoted track
    # plays, which is misleading.
    if player.current is None and not player.queue:
        key = "skip.success.queue_empty"
    else:
        key = "skip.success.with_queue"
    await ctx.respond(t(key, locale=locale_for_public(ctx), title=safe_title))
