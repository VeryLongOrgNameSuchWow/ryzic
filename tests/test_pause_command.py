"""Tests for ``ryzic.commands.pause``."""

from __future__ import annotations

from typing import Any, cast

import pytest

from ryzic import lavalink_glue
from ryzic.commands import pause as pause_module
from ryzic.i18n import t
from tests._command_helpers import (
    FakeAudioTrack,
    FakeBot,
    FakeLavalinkClient,
    both_in_voice,
    context_for,
    install_lavalink_client,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    install_lavalink_client(None)


async def test_voice_precondition_short_circuits() -> None:
    bot = FakeBot()  # bot not in voice
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await pause_module._handle_pause(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await pause_module._handle_pause(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await pause_module._handle_pause(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")


async def test_player_idle_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = None

    await pause_module._handle_pause(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert player.set_pause_calls == []


async def test_already_paused_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack()
    player.paused = True

    await pause_module._handle_pause(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("pause.error.already_paused", locale="en_US")
    assert fake.responses[0][0] == "Already paused. Use /resume."
    assert fake.responses[0][1].get("ephemeral") is True
    assert player.set_pause_calls == []


async def test_pause_success_is_public() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack()

    await pause_module._handle_pause(ctx)

    assert player.set_pause_calls == [True]
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("pause.success.paused", locale="en_US")
    assert fake.responses[0][0] == "Paused."
    assert "ephemeral" not in fake.responses[0][1]


def test_pause_loader_registered() -> None:
    assert pause_module.Pause._command_data.name == "pause"
    assert pause_module.Pause._command_data.description == t(
        "pause.command.description", locale="en_US"
    )
    assert pause_module.Pause._command_data.description == "Pause the currently-playing track."
