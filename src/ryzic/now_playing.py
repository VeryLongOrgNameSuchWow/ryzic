"""Persistent now-playing controller embed (issue #90).

A long-lived embed in the channel where ``/play`` was last invoked,
showing the currently-playing track plus three media-remote buttons
(pause/resume, skip, leave). Updates on every play/pause/resume/skip/end
so the channel always carries an accurate current-state surface.

Channel choice follows :data:`lavalink_glue.last_play_channel`: the
embed lives where the conversation that started playback lives. This
avoids a new env var and keeps the controller contextually present
(rather than splitting attention across "where commands run" vs. "where
state lives"). When ``/play`` lands in a new channel within the same
guild, the prior controller is torn down and a fresh one is posted in
the new channel — there's only ever one controller per guild.

Buttons drive the same code paths as the equivalent slash commands
(``_handle_pause`` / ``_handle_resume`` / ``_handle_skip`` /
``_handle_leave``) via the :class:`InteractionContextLike` adapter,
which wraps a hikari ``ComponentInteraction`` in the slim surface those
handlers read off ``lightbulb.Context`` (``guild_id`` / ``user`` /
``channel_id`` / ``client.app`` / ``respond``).

State is in-memory (mirrors the rest of ``lavalink_glue``'s singletons).
A bot restart drops the mapping but the actual posted message persists
in the channel — a stale-button click on a post-restart embed gets the
ephemeral graceful-failure path (see :func:`is_known_message`).
"""

from __future__ import annotations

import asyncio
import logging

import hikari

from . import lavalink_glue, ux
from .i18n import t

_log = logging.getLogger(__name__)


# Custom IDs are namespaced so ``InteractionCreateEvent`` filtering can
# cheaply ignore unrelated buttons (forward-compat with future component
# surfaces). The ``ryzic:np:`` prefix is short enough that a 100-char
# Discord custom_id ceiling is not a concern.
BUTTON_PAUSE = "ryzic:np:pause"
BUTTON_RESUME = "ryzic:np:resume"
BUTTON_SKIP = "ryzic:np:skip"
# custom_id wire-compat preserved per #147 and #174; user-visible label
# is "Stop & leave" (see ``_LABEL_LEAVE`` below). Existing controller
# embeds posted before the label rename remain dispatchable.
BUTTON_STOP = "ryzic:np:stop"

# All of our custom_ids share this prefix; the interaction listener
# filters with ``startswith`` to short-circuit cheap.
_CUSTOM_ID_PREFIX = "ryzic:np:"

# Module-import-time button labels — locale is hard-coded en_US because
# controller renders are event-driven (no ctx). Same shape as the wave's
# Option A pattern for slash-command descriptions.
_LABEL_PAUSE = t("controller.button.pause", locale="en_US")
_LABEL_RESUME = t("controller.button.resume", locale="en_US")
_LABEL_SKIP = t("controller.button.skip", locale="en_US")
_LABEL_LEAVE = t("controller.button.leave", locale="en_US")


# Per-guild ``(channel_id, message_id)`` of the active controller. Module
# scope mirrors ``lavalink_glue`` — there's exactly one mapping per guild
# and an extra registry layer would be ceremony. Tests reset via
# :func:`_reset_state_for_test`.
_controllers: dict[int, tuple[int, int]] = {}


def is_known_message(guild_id: int, message_id: int) -> bool:
    """Return True if ``message_id`` is the active controller for ``guild_id``.

    Used by the interaction handler to distinguish "live controller" from
    "stale embed left behind after restart" — the latter gets an
    ephemeral graceful-failure rather than a spurious player command.
    """
    record = _controllers.get(guild_id)
    return record is not None and record[1] == message_id


def _build_components() -> list[hikari.api.MessageActionRowBuilder]:
    """Build the 3-button media-remote row.

    Order matches the issue body's spec: pause/resume · skip · leave.
    The pause/resume button surfaces both states behind one custom_id
    pair — the renderer picks which custom_id to bind based on player
    state, so the user always sees one of the two icons at a time.
    """
    row = hikari.impl.MessageActionRowBuilder()
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_PAUSE,
        emoji="⏸️",  # ⏸
        label=_LABEL_PAUSE,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_SKIP,
        emoji="⏭️",  # ⏭
        label=_LABEL_SKIP,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.DANGER,
        BUTTON_STOP,
        emoji="⏹️",  # ⏹
        label=_LABEL_LEAVE,
    )
    return [row]


def _build_components_paused() -> list[hikari.api.MessageActionRowBuilder]:
    """Same row but with Resume in place of Pause."""
    row = hikari.impl.MessageActionRowBuilder()
    row.add_interactive_button(
        hikari.ButtonStyle.SUCCESS,
        BUTTON_RESUME,
        emoji="▶️",  # ▶
        label=_LABEL_RESUME,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_SKIP,
        emoji="⏭️",  # ⏭
        label=_LABEL_SKIP,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.DANGER,
        BUTTON_STOP,
        emoji="⏹️",  # ⏹
        label=_LABEL_LEAVE,
    )
    return [row]


def _build_idle_components() -> list[hikari.api.MessageActionRowBuilder]:
    """All buttons disabled — used by the post-queue 'idle' rendering."""
    row = hikari.impl.MessageActionRowBuilder()
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_PAUSE,
        emoji="⏸️",
        label=_LABEL_PAUSE,
        is_disabled=True,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_SKIP,
        emoji="⏭️",
        label=_LABEL_SKIP,
        is_disabled=True,
    )
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY,
        BUTTON_STOP,
        emoji="⏹️",
        label=_LABEL_LEAVE,
        is_disabled=True,
    )
    return [row]


async def upsert_for_track_start(bot: hikari.GatewayBot, guild_id: int) -> None:
    """Render (or post) the controller for a guild that just started a track.

    Called from ``lavalink_glue.on_track_start``. This is the only
    creation point — :func:`refresh` short-circuits when there's no
    existing controller record so /pause / /resume / button clicks don't
    spawn a controller in unrelated channels.

    No-ops when there's no ``last_play_channel`` mapping yet — the
    controller follows ``/play`` (the channel comes from the user's
    most recent ``/play`` interaction).
    """
    channel_id = lavalink_glue.last_play_channel.get(guild_id)
    if channel_id is None:
        return
    await _render_for_player(bot, guild_id, channel_id)


async def refresh(bot: hikari.GatewayBot, guild_id: int) -> None:
    """Re-render the existing controller; no-op if none has been posted.

    Called from /pause, /resume, /leave, button handlers, and the
    queue-end / 4014 cleanup paths so every user-visible state transition
    propagates to the channel embed. Refusing to create a controller
    here is intentional — only :func:`upsert_for_track_start` (driven
    by lavalink TrackStart) is allowed to post a new one.
    """
    record = _controllers.get(guild_id)
    if record is None:
        return
    channel_id = record[0]
    await _render_for_player(bot, guild_id, channel_id)


async def refresh_all(bot: hikari.GatewayBot) -> None:
    """Refresh all active controllers whose progress is actually moving.

    Used by the bot's background loop to advance progress bars. Skips
    paused / idle / disconnected players so we don't issue byte-identical
    edits every cycle (their state only changes via an event, which
    already routes through :func:`refresh`). Staggers updates to spread
    concurrent in-flight REST requests.
    """
    for guild_id in list(_controllers.keys()):
        player = lavalink_glue.get_player(guild_id)
        if player is None or player.paused or player.current is None:
            continue
        try:
            await refresh(bot, guild_id)
            await asyncio.sleep(0.1)
        except Exception:
            # refresh()/_post_or_edit already logs HikariErrors (the
            # expected REST failures). Anything reaching here is an
            # unexpected bug — log it so it isn't silently lost.
            _log.debug("periodic refresh failed for guild %d", guild_id, exc_info=True)


async def _render_for_player(bot: hikari.GatewayBot, guild_id: int, channel_id: int) -> None:
    """Pull current player state and render the controller into ``channel_id``."""
    player = lavalink_glue.get_player(guild_id)
    if player is None or player.current is None:
        await _render_idle(bot, guild_id, channel_id)
        return

    info = ux.get_track_info(player.current)
    if info is None:
        # Track has no attached metadata; the embed would render
        # half-populated. Treat as idle until a metadata-bearing track
        # plays.
        await _render_idle(bot, guild_id, channel_id)
        return

    embed = ux.build_now_playing_embed(
        info,
        position_ms=player.position,
        paused=player.paused,
        queue_length=len(player.queue),
        locale="en_US",
    )
    components = _build_components_paused() if player.paused else _build_components()
    await _post_or_edit(bot, guild_id, channel_id, embed=embed, components=components)


async def _render_idle(bot: hikari.GatewayBot, guild_id: int, channel_id: int) -> None:
    """Render the 'queue empty / nothing playing' state with disabled buttons."""
    embed = ux.build_now_playing_idle_embed(locale="en_US")
    await _post_or_edit(
        bot,
        guild_id,
        channel_id,
        embed=embed,
        components=_build_idle_components(),
    )


async def _post_or_edit(
    bot: hikari.GatewayBot,
    guild_id: int,
    channel_id: int,
    *,
    embed: hikari.Embed,
    components: list[hikari.api.MessageActionRowBuilder],
) -> None:
    """Edit the existing controller in-place, or post a new one.

    When the recorded channel doesn't match ``channel_id`` (a follow-up
    ``/play`` happened in a different text channel), the prior controller
    is left in place and a fresh one is posted in the new channel — we
    don't try to delete the old one so the audit trail stays visible.
    The mapping is updated so subsequent edits target the new message.
    """
    record = _controllers.get(guild_id)

    if record is not None and record[0] == channel_id:
        prior_channel, message_id = record
        try:
            await bot.rest.edit_message(
                prior_channel, message_id, embed=embed, components=components
            )
            return
        except hikari.NotFoundError:
            # Message was deleted out from under us; fall through to repost.
            _controllers.pop(guild_id, None)
        except hikari.HikariError:
            _log.exception(
                "guild=%d failed to edit now-playing controller %d/%d",
                guild_id,
                prior_channel,
                message_id,
            )
            return

    try:
        message = await bot.rest.create_message(channel_id, embed=embed, components=components)
    except hikari.HikariError:
        _log.exception(
            "guild=%d failed to post now-playing controller in channel %d",
            guild_id,
            channel_id,
        )
        return
    _controllers[guild_id] = (channel_id, int(message.id))


async def teardown(bot: hikari.GatewayBot, guild_id: int) -> None:
    """Drop the controller record + render an idle final state.

    Called from ``/leave`` and the voice-4014/node-disconnect cleanup
    paths. The message itself is left in the channel as a "history
    marker" of the last session; we just stop tracking it for edits.
    """
    record = _controllers.pop(guild_id, None)
    if record is None:
        return
    channel_id, message_id = record
    try:
        await bot.rest.edit_message(
            channel_id,
            message_id,
            embed=ux.build_now_playing_idle_embed(locale="en_US"),
            components=_build_idle_components(),
        )
    except hikari.NotFoundError:
        return
    except hikari.HikariError:
        _log.exception(
            "guild=%d failed to finalize now-playing controller %d/%d",
            guild_id,
            channel_id,
            message_id,
        )


def _reset_state_for_test() -> None:
    """Test-only: clear all per-guild controller state."""
    _controllers.clear()
