"""``/nowplaying`` slash command: show the currently-playing track.

``/nowplaying`` (and its ``/np`` alias) are ephemeral-by-default: the
``private`` option defaults to ``True``, routing the success embed to an
ephemeral response so the invoker can check the current track without
spamming a busy channel. Empty/error paths stay ephemeral regardless —
failure messages don't pollute the channel either way.

Issue #148/#159 aligned ``/queue`` this way (status-introspection
commands answer the invoker, not the channel); issue #207 extended the
alignment to ``/nowplaying``. The original mirror-intent was #100.

Issue #150: primary command is ``/nowplaying``; ``/np`` is preserved as a
shorthand. Lightbulb v3 has no first-class alias parameter on
``SlashCommand``, so the alias is implemented as a second registered
class that delegates to the same ``_handle_np`` handler.
"""

from __future__ import annotations

import hikari
import lightbulb

from .. import lavalink_glue, ux
from ..i18n import locale_for_ephemeral, locale_for_public, t

loader = lightbulb.Loader()


@loader.command
class NowPlaying(
    lightbulb.SlashCommand,
    name="nowplaying",
    description=t("np.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    private = lightbulb.boolean(
        "private",
        t("common.param.private.description", locale="en_US"),
        default=True,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_np(ctx, private=self.private)


@loader.command
class Np(
    lightbulb.SlashCommand,
    name="np",
    description=t("np.command.description", locale="en_US"),
    contexts=[hikari.ApplicationContextType.GUILD],
):
    private = lightbulb.boolean(
        "private",
        t("common.param.private.description", locale="en_US"),
        default=True,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_np(ctx, private=self.private)


async def _handle_np(ctx: lightbulb.Context, *, private: bool = True) -> None:
    guild_id = ctx.guild_id
    if guild_id is None:
        await ctx.respond(
            t(
                "voice.error.run_in_server",
                locale=locale_for_ephemeral(ctx),
                command=ctx.command_data.name,
            ),
            ephemeral=True,
        )
        return

    player = lavalink_glue.get_player(guild_id)
    if player is None or player.current is None:
        await ctx.respond(
            t("np.error.nothing_playing", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    info = ux.get_track_info(player.current)
    if info is None:
        await ctx.respond(
            t("np.error.nothing_playing", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    locale = locale_for_ephemeral(ctx) if private else locale_for_public(ctx)
    embed = ux.build_simple_now_playing_embed(
        info, int(player.position), paused=player.paused, locale=locale
    )
    await ctx.respond(embed=embed, ephemeral=private)
