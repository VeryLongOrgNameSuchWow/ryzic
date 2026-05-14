"""``/nowplaying`` slash command: show the currently-playing track.

Mirrors ``/queue``'s ``private`` flag (issue #100): ``private=True`` routes
the success embed to an ephemeral response so the invoker can check the
current track without spamming a busy channel. Empty/error paths stay
ephemeral regardless — failure messages don't pollute the channel
either way.

Issue #150: primary command is ``/nowplaying``; ``/np`` is preserved as a
shorthand. Lightbulb v3 has no first-class alias parameter on
``SlashCommand``, so the alias is implemented as a second registered
class that delegates to the same ``_handle_np`` handler.
"""

from __future__ import annotations

from typing import cast

import hikari
import lavalink
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
        t("np.param.private.description", locale="en_US"),
        default=False,
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
        t("np.param.private.description", locale="en_US"),
        default=False,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await _handle_np(ctx, private=self.private)


async def _handle_np(ctx: lightbulb.Context, *, private: bool = False) -> None:
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

    ll_client = lavalink_glue.get_lavalink_client()
    if ll_client is None:
        await ctx.respond(
            t("np.error.nothing_playing", locale=locale_for_ephemeral(ctx)),
            ephemeral=True,
        )
        return

    player = cast(lavalink.DefaultPlayer | None, ll_client.player_manager.get(guild_id))
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

    # ``private=True`` → ephemeral response (use ephemeral locale resolver);
    # ``private=False`` → public response (use guild-preferred locale).
    locale = locale_for_ephemeral(ctx) if private else locale_for_public(ctx)
    embed = ux.build_simple_now_playing_embed(
        info, int(player.position), paused=player.paused, locale=locale
    )
    await ctx.respond(embed=embed, ephemeral=private)
