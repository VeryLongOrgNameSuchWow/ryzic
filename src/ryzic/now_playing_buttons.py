"""Hikari interaction handler for the now-playing controller buttons.

Routes ⏯ Pause/Resume · ⏭ Skip · ⏹ Stop button clicks to the same
``_handle_*`` bodies the slash commands use, via a slim
:class:`InteractionContextLike` adapter that exposes the surface those
handlers read off ``lightbulb.Context`` (``guild_id``, ``user``,
``channel_id``, ``client.app``, ``respond``).

Voice-presence checks apply identically to button presses — the
``_handle_*`` bodies invoke ``ensure_same_voice`` themselves, so the
guard fires through the adapter without duplicate logic here. Issue
#90's hard line: ``no duplicated transport logic``.

A click on a stale embed (post-restart, controller record gone) gets a
graceful ephemeral failure rather than a spurious player command — see
:func:`now_playing.is_known_message`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import hikari
import lightbulb

from . import now_playing
from .commands.leave import _handle_leave
from .commands.pause import _handle_pause
from .commands.resume import _handle_resume
from .commands.skip import _handle_skip

_log = logging.getLogger(__name__)


_DISPATCH = {
    now_playing.BUTTON_PAUSE: _handle_pause,
    now_playing.BUTTON_RESUME: _handle_resume,
    now_playing.BUTTON_SKIP: _handle_skip,
    now_playing.BUTTON_STOP: _handle_leave,
}


class _InteractionUser:
    """Minimal ``ctx.user`` stand-in for the adapter."""

    def __init__(self, interaction: hikari.ComponentInteraction) -> None:
        # ``user`` always exists on guild interactions; ``member.user`` is
        # equivalent and present on non-guild paths too. Prefer ``user``
        # for parity with lightbulb's resolution.
        self.id = int(interaction.user.id)
        self.username = interaction.user.username


class _InteractionLightbulbClient:
    """Adapter exposing only ``app`` for ``ensure_same_voice``."""

    def __init__(self, app: hikari.GatewayBot) -> None:
        self.app = app


class InteractionContextLike:
    """Adapter wrapping ``ComponentInteraction`` in the slash-context surface.

    Only exposes what the four handlers actually read:
    ``guild_id`` / ``user`` / ``channel_id`` / ``client.app`` / ``respond``.
    Cast to ``lightbulb.Context`` for the dispatch call so type-checking
    doesn't widen across the call site.

    ``respond`` maps to ``create_initial_response`` on first call and
    ``edit_initial_response`` thereafter — same lifecycle as
    lightbulb's. ``ephemeral=True`` toggles ``MessageFlag.EPHEMERAL``.
    """

    def __init__(self, interaction: hikari.ComponentInteraction, app: hikari.GatewayBot) -> None:
        self._interaction = interaction
        self.guild_id = int(interaction.guild_id) if interaction.guild_id else None
        self.user = _InteractionUser(interaction)
        self.channel_id = int(interaction.channel_id)
        self.client = _InteractionLightbulbClient(app)
        self._initial_sent = False

    async def respond(self, content: Any = None, **kwargs: Any) -> None:
        ephemeral = bool(kwargs.pop("ephemeral", False))
        flags = hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.MessageFlag.NONE
        if not self._initial_sent:
            self._initial_sent = True
            await self._interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content=content,
                flags=flags,
                **kwargs,
            )
            return
        # Subsequent responses: lightbulb edits the deferred response.
        # Our handlers respond once, but keep the edit path for parity.
        await self._interaction.edit_initial_response(content=content, **kwargs)


async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle one interaction event; dispatch button clicks to the right handler."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    custom_id = interaction.custom_id
    if not custom_id.startswith(now_playing._CUSTOM_ID_PREFIX):
        return
    handler = _DISPATCH.get(custom_id)
    if handler is None:
        # Unknown ryzic:np:* custom_id (forward-compat: a future button
        # added without a dispatch entry). Acknowledge silently.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="That button isn't wired up.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    if interaction.guild_id is None:
        # Components on a guild controller can't reach DMs in practice,
        # but the dispatch handlers all assume guild context — fail safe.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="Controller buttons only work in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    if not now_playing.is_known_message(int(interaction.guild_id), int(interaction.message.id)):
        # Stale embed (bot restarted; controller record dropped). Per
        # issue #90 hard line: ephemeral graceful failure.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="This controller is from a previous session. Run /play to start a new one.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    bot = cast(hikari.GatewayBot, event.app)
    adapter = cast(lightbulb.Context, InteractionContextLike(interaction, bot))
    try:
        await handler(adapter)
    except Exception:
        _log.exception(
            "guild=%d controller-button %s handler failed",
            interaction.guild_id,
            custom_id,
        )
        # Best-effort failure surface; the underlying handler may have
        # already responded.
        try:
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="Something went wrong. Try the slash command directly.",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
        except hikari.HikariError:
            return
        return

    # After the handler ran, refresh the controller so the embed
    # reflects the new state (paused state flipped, queue advanced,
    # etc.). Lavalink TrackStart will already have refreshed for /skip,
    # but pause/resume don't fire any lavalink event, so we drive it
    # explicitly here.
    await now_playing.refresh(bot, int(interaction.guild_id))


def register_listener(bot: hikari.GatewayBot) -> None:
    """Subscribe :func:`on_interaction` to ``hikari.InteractionCreateEvent``."""
    bot.subscribe(hikari.InteractionCreateEvent, on_interaction)
