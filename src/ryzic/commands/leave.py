"""``/leave`` slash command (M1 §3).

Stops playback, clears the queue, and disconnects from voice. Cleanup
goes through ``_teardown_player_session``, the same helper used by the
auto-leave timer and the 4014 close paths; the guild-leave path
releases the same set inline (a REST teardown would 403 once the bot
has lost the guild). The shared set: queued audio-cache pins, the
now-playing controller, the auto-leave task, the voice-ready handshake
event.
"""

from __future__ import annotations

from typing import cast

import hikari
import lightbulb

from .. import lavalink_glue
from ..i18n import locale_for_ephemeral, locale_for_public, t
from ..voice_check import ensure_same_voice

loader = lightbulb.Loader()


@loader.command
class Leave(
    lightbulb.SlashCommand,
    name="leave",
    description=t("leave.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_leave(ctx)


async def _handle_leave(ctx: lightbulb.Context) -> None:
    guild_id = cast(int, ctx.guild_id)

    # Take ownership of teardown before the first await. _cancel_auto_leave
    # stops a still-sleeping timer; begin_explicit_leave is the backstop for
    # a timer whose sleep already completed this tick and self-popped before
    # we ran — _auto_leave sees the marker and bows out instead of
    # broadcasting after the explicit leave (#218).
    lavalink_glue._cancel_auto_leave(guild_id)
    lavalink_glue.begin_explicit_leave(guild_id)
    try:
        if await ensure_same_voice(ctx) is None:
            return

        player = lavalink_glue.get_player(guild_id)
        if player is None or not player.is_connected:
            await ctx.respond(
                t("voice.error.bot_not_in_voice", locale=locale_for_ephemeral(ctx)),
                ephemeral=True,
            )
            return

        await player.stop()

        bot = cast(hikari.GatewayBot, ctx.client.app)
        # Mark as intentional so on_websocket_closed skips voice_lost broadcast.
        lavalink_glue.mark_intentional_disconnect(guild_id)
        try:
            await bot.update_voice_state(guild_id, None)
        except Exception:
            lavalink_glue.clear_intentional_disconnect(guild_id)
            raise

        await lavalink_glue._teardown_player_session(bot, guild_id, player)
        await ctx.respond(t("leave.success.left", locale=locale_for_public(ctx)))
    finally:
        lavalink_glue.end_explicit_leave(guild_id)
