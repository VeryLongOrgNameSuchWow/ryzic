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
        # ``rest`` is used by now_playing's edit/create — not by the
        # button handler itself in these tests, but the post-handler
        # ``now_playing.refresh`` will call it.
        self.rest = MagicMock()

        async def _create(*args: Any, **kwargs: Any) -> Any:
            m = MagicMock()
            m.id = 9000
            return m

        async def _edit(*args: Any, **kwargs: Any) -> Any:
            m = MagicMock()
            m.id = 9000
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
