"""Hikari interaction handler for the now-playing controller buttons.

Routes ⏯ Pause/Resume · ⏭ Skip · ⏹ Stop & leave button clicks to the same
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
from .i18n import locale_for_ephemeral, t

_log = logging.getLogger(__name__)


_DISPATCH = {
    now_playing.BUTTON_PAUSE: _handle_pause,
    now_playing.BUTTON_RESUME: _handle_resume,
    now_playing.BUTTON_SKIP: _handle_skip,
    now_playing.BUTTON_STOP: _handle_leave,
}


class _InteractionUser:
    """Minimal ``ctx.user`` stand-in for the adapter.

    Only ``id`` is exposed — the four dispatch handlers (pause / resume
    / skip / leave) read just the user id (``ensure_same_voice`` cares
    about voice-state lookup; the slash bodies don't render a username
    anywhere). Adding ``.username`` would be dead surface today.
    """

    def __init__(self, interaction: hikari.ComponentInteraction) -> None:
        # ``user`` always exists on guild interactions; ``member.user`` is
        # equivalent and present on non-guild paths too. Prefer ``user``
        # for parity with lightbulb's resolution.
        self.id = int(interaction.user.id)


class _InteractionLightbulbClient:
    """Adapter exposing only ``app`` for ``ensure_same_voice``."""

    def __init__(self, app: hikari.GatewayBot) -> None:
        self.app = app


class InteractionContextLike:
    """Adapter wrapping ``ComponentInteraction`` in the slash-context surface.

    Exposes what the four handlers actually read:
    ``guild_id`` / ``user`` / ``channel_id`` / ``client.app`` / ``respond``
    plus ``interaction`` (used by ``ryzic.i18n.locale_for_ephemeral`` /
    ``locale_for_public`` via ``getattr``-with-default for the locale code).
    Cast to ``lightbulb.Context`` for the dispatch call so type-checking
    doesn't widen across the call site.

    ``respond`` maps to ``create_initial_response`` on first call and
    ``edit_initial_response`` thereafter — same lifecycle as
    lightbulb's. ``ephemeral=True`` toggles ``MessageFlag.EPHEMERAL``.
    """

    def __init__(self, interaction: hikari.ComponentInteraction, app: hikari.GatewayBot) -> None:
        self._interaction = interaction
        # Public alias so locale resolvers (and any future ctx-shaped
        # consumer) can reach ``.locale`` / ``.guild_locale`` the same
        # way they would on a real lightbulb ``ctx``.
        self.interaction = interaction
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
    bot = cast(hikari.GatewayBot, event.app)
    adapter = InteractionContextLike(interaction, bot)
    locale = locale_for_ephemeral(cast(lightbulb.Context, adapter))
    handler = _DISPATCH.get(custom_id)
    if handler is None:
        # Unknown ryzic:np:* custom_id (forward-compat: a future button
        # added without a dispatch entry). Acknowledge silently.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content=t("controller.error.unknown_button", locale=locale),
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    if interaction.guild_id is None:
        # Components on a guild controller can't reach DMs in practice,
        # but the dispatch handlers all assume guild context — fail safe.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content=t("controller.error.guild_only", locale=locale),
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    if not now_playing.is_known_message(int(interaction.guild_id), int(interaction.message.id)):
        # Stale embed (bot restarted; controller record dropped). Per
        # issue #90 hard line: ephemeral graceful failure.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content=t("controller.error.stale_session", locale=locale),
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    try:
        await handler(cast(lightbulb.Context, adapter))
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
                content=t("controller.error.handler_failed", locale=locale),
                flags=hikari.MessageFlag.EPHEMERAL,
            )
        except hikari.HikariError:
            return
        return

    # NOTE: do NOT call ``now_playing.refresh`` here. The slash bodies
    # already drive every state change to the embed:
    # - PAUSE / RESUME: ``_handle_pause`` / ``_handle_resume`` end with
    #   their own ``now_playing.refresh`` call.
    # - SKIP: ``lavalink.TrackStartEvent`` fires when the next track
    #   begins (sub-second on a healthy node) and triggers
    #   ``upsert_for_track_start``. Empty-queue case lands on
    #   ``QueueEndEvent`` → ``refresh`` → idle render.
    # - STOP: ``_handle_leave`` calls ``now_playing.teardown`` which
    #   pops the registry record; a follow-up refresh would no-op.
    # An extra refresh here would double the per-click ``edit_message``
    # REST traffic and halve the effective per-message rate-limit
    # budget (Discord caps edits at 5 / 5s).


def register_listener(bot: hikari.GatewayBot) -> None:
    """Subscribe :func:`on_interaction` to ``hikari.InteractionCreateEvent``."""
    bot.subscribe(hikari.InteractionCreateEvent, on_interaction)
