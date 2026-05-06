"""Tests for ``ryzic.now_playing_buttons`` interaction handler.

These exercise the dispatch + adapter contract: a button click reaches
the right slash-command body via the ``InteractionContextLike`` adapter,
voice-presence checks fire identically, and stale-controller clicks
get a graceful ephemeral.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import hikari
import pytest

from ryzic import lavalink_glue, now_playing, now_playing_buttons
from tests._command_helpers import (
    FakeCache,
    FakeLavalinkClient,
    FakeUser,
    FakeVoiceState,
    install_lavalink_client,
    make_track_with_info,
)


def _make_interaction(
    *,
    custom_id: str,
    guild_id: int | None = 111,
    message_id: int = 9000,
    user_id: int = 222,
    channel_id: int = 555,
) -> Any:
    """Build a ``ComponentInteraction``-shaped MagicMock.

    ``MagicMock(spec=...)`` makes ``isinstance`` calls succeed against
    the spec class without dragging in attrs-based property machinery
    that blocks direct attribute assignment. Captured calls live on
    ``responses`` / ``edit_calls`` so assertions can inspect content.
    """
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.custom_id = custom_id
    interaction.guild_id = guild_id
    interaction.user = FakeUser(user_id)
    interaction.channel_id = channel_id
    interaction.message = MagicMock()
    interaction.message.id = message_id
    interaction.responses = []
    interaction.edit_calls = []

    async def _create(
        response_type: hikari.ResponseType, content: Any = None, **kwargs: Any
    ) -> None:
        interaction.responses.append({"content": content, **kwargs})

    async def _edit(content: Any = None, **kwargs: Any) -> None:
        interaction.edit_calls.append({"content": content, **kwargs})

    interaction.create_initial_response.side_effect = _create
    interaction.edit_initial_response.side_effect = _edit
    return interaction


class _FakeBot:
    """Minimal hikari.GatewayBot stand-in for the adapter tests."""

    def __init__(
        self,
        *,
        bot_user_id: int = 10,
        states: dict[tuple[int, int], FakeVoiceState] | None = None,
    ) -> None:
        self._me = FakeUser(bot_user_id)
        self.cache = FakeCache(states or {})
        self.update_voice_state_calls: list[tuple[int, int | None]] = []
        # Public lists let tests count REST traffic per click — see the
        # "1 click = 1 edit" regression test below.
        self.create_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.rest = MagicMock()

        async def _create(channel_id: int, **kwargs: Any) -> Any:
            self.create_calls.append({"channel_id": channel_id, **kwargs})
            m = MagicMock()
            m.id = 9000
            return m

        async def _edit(channel_id: int, message_id: int, **kwargs: Any) -> Any:
            self.edit_calls.append({"channel_id": channel_id, "message_id": message_id, **kwargs})
            m = MagicMock()
            m.id = message_id
            return m

        self.rest.create_message = _create
        self.rest.edit_message = _edit

    def get_me(self) -> FakeUser:
        return self._me

    async def update_voice_state(self, *args: Any, **kwargs: Any) -> None:
        self.update_voice_state_calls.append(args)


def _both_in_voice(channel_id: int = 999, *, guild_id: int = 111) -> _FakeBot:
    return _FakeBot(
        states={
            (guild_id, 10): FakeVoiceState(channel_id=channel_id),
            (guild_id, 222): FakeVoiceState(channel_id=channel_id),
        },
    )


def _event(interaction: Any, bot: _FakeBot) -> hikari.InteractionCreateEvent:
    event = MagicMock(spec=hikari.InteractionCreateEvent)
    event.interaction = interaction
    event.app = bot
    return cast(hikari.InteractionCreateEvent, event)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    now_playing._reset_state_for_test()
    install_lavalink_client(None)


# ---------------------------------------------------------------------------
# Filtering: non-button / unrelated-prefix interactions
# ---------------------------------------------------------------------------


async def test_non_component_interaction_is_ignored() -> None:
    """Slash-command interactions and similar must pass through untouched."""
    event = MagicMock(spec=hikari.InteractionCreateEvent)
    event.interaction = MagicMock()  # not a ComponentInteraction subclass
    # no exception, no response

    await now_playing_buttons.on_interaction(event)


async def test_unrelated_custom_id_is_ignored() -> None:
    bot = _both_in_voice()
    interaction = _make_interaction(custom_id="some_other:button:foo")

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert interaction.responses == []


# ---------------------------------------------------------------------------
# Stale controller click
# ---------------------------------------------------------------------------


async def test_stale_controller_click_returns_graceful_ephemeral() -> None:
    bot = _both_in_voice()
    # No controller record installed → message is unknown.
    interaction = _make_interaction(custom_id=now_playing.BUTTON_PAUSE, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert len(interaction.responses) == 1
    response = interaction.responses[0]
    assert "previous session" in str(response["content"])
    assert response["flags"] == hikari.MessageFlag.EPHEMERAL


# ---------------------------------------------------------------------------
# Voice gating: button presses respect ensure_same_voice
# ---------------------------------------------------------------------------


async def test_pause_button_blocked_by_voice_check_when_user_not_in_voice() -> None:
    """Voice-presence check applies to button presses identically."""
    bot = _FakeBot(
        states={
            (111, 10): FakeVoiceState(channel_id=999),  # bot in voice
            # user has no voice state → ensure_same_voice rejects
        },
    )
    install_lavalink_client(FakeLavalinkClient())
    now_playing._controllers[111] = (555, 9000)  # mark as known
    interaction = _make_interaction(custom_id=now_playing.BUTTON_PAUSE, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    # The voice-check ephemeral should have fired.
    assert len(interaction.responses) >= 1
    assert "Join" in str(interaction.responses[0]["content"])
    assert interaction.responses[0]["flags"] == hikari.MessageFlag.EPHEMERAL


# ---------------------------------------------------------------------------
# Successful dispatch: pause and resume
# ---------------------------------------------------------------------------


async def test_pause_button_calls_pause_handler_and_refreshes() -> None:
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_PAUSE, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.set_pause_calls == [True]
    # First response is the "Paused." message (slash-command body).
    assert interaction.responses[0]["content"] == "Paused."


async def test_resume_button_calls_resume_handler() -> None:
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    player.paused = True
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_RESUME, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.set_pause_calls == [False]
    assert interaction.responses[0]["content"] == "Resumed."


async def test_skip_button_calls_skip_handler() -> None:
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="ToSkip")
    player.queue = [make_track_with_info(title="Next", video_id="aaaaaaaaaaa")]
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_SKIP, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.skip_calls == 1
    assert "Skipped" in str(interaction.responses[0]["content"])


async def test_stop_button_calls_leave_handler() -> None:
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_STOP, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.stop_calls == 1
    assert "Left voice channel" in str(interaction.responses[0]["content"])


# ---------------------------------------------------------------------------
# Adapter ephemeral handling
# ---------------------------------------------------------------------------


async def test_adapter_propagates_ephemeral_flag() -> None:
    """Slash bodies that respond with ephemeral=True must surface the flag."""
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    # No player → /pause body responds "Nothing is playing." ephemerally.
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_PAUSE, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    response = interaction.responses[0]
    assert response["content"] == "Nothing is playing."
    assert response["flags"] == hikari.MessageFlag.EPHEMERAL


# ---------------------------------------------------------------------------
# Rate-limit regression: 1 click = 1 edit (issue #90 reviewer must-fix)
# ---------------------------------------------------------------------------


async def test_pause_click_produces_exactly_one_edit_message() -> None:
    """One button press → at most one ``edit_message`` REST call.

    Pinned because both ``_handle_pause`` (slash body) and the
    interaction handler used to call ``now_playing.refresh`` after the
    state change. The doubled refresh halved the effective per-message
    edit-rate budget (Discord caps at 5 / 5s). The interaction handler
    no longer post-refreshes; the slash body's own refresh is the
    single source of truth for the embed update.
    """
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_PAUSE, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.set_pause_calls == [True]
    # The single ``edit_message`` call comes from ``_handle_pause`` →
    # ``now_playing.refresh`` → ``_post_or_edit``. Adding any more
    # refresh calls (post-handler in ``on_interaction`` or anywhere
    # else) would fail this assertion.
    assert len(bot.edit_calls) == 1
    assert bot.edit_calls[0]["channel_id"] == 555
    assert bot.edit_calls[0]["message_id"] == 9000


async def test_resume_click_produces_exactly_one_edit_message() -> None:
    """Same invariant for the resume path."""
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    player.paused = True
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_RESUME, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.set_pause_calls == [False]
    assert len(bot.edit_calls) == 1


async def test_skip_click_does_not_redundantly_edit_via_button_handler() -> None:
    """SKIP relies on ``TrackStartEvent``/``QueueEndEvent`` for the embed
    update — the button handler itself must NOT call refresh.

    Tests run without a live lavalink client driving events, so any
    edit observed here would be from the (now-removed) post-handler
    ``now_playing.refresh`` in ``on_interaction``. ``_handle_skip``
    itself does not refresh.
    """
    bot = _both_in_voice()
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="X")
    player.queue = [make_track_with_info(title="Next", video_id="aaaaaaaaaaa")]
    lavalink_glue.last_play_channel[111] = 555
    now_playing._controllers[111] = (555, 9000)
    interaction = _make_interaction(custom_id=now_playing.BUTTON_SKIP, message_id=9000)

    await now_playing_buttons.on_interaction(_event(interaction, bot))

    assert player.skip_calls == 1
    assert bot.edit_calls == []
