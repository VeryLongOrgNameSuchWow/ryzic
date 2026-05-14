"""Tests for ``ryzic.commands.resume``."""

from __future__ import annotations

from typing import Any, cast

import pytest

from ryzic import lavalink_glue
from ryzic.commands import resume as resume_module
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
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await resume_module._handle_resume(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await resume_module._handle_resume(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await resume_module._handle_resume(ctx)

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

    await resume_module._handle_resume(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert player.set_pause_calls == []


async def test_already_playing_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack()
    player.paused = False

    await resume_module._handle_resume(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("resume.error.already_playing", locale="en_US")
    assert fake.responses[0][0] == "Already playing. Use /pause to pause."
    assert fake.responses[0][1].get("ephemeral") is True
    assert player.set_pause_calls == []


async def test_resume_success_is_public() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack()
    player.paused = True

    await resume_module._handle_resume(ctx)

    assert player.set_pause_calls == [False]
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("resume.success.resumed", locale="en_US")
    assert fake.responses[0][0] == "Resumed."
    assert "ephemeral" not in fake.responses[0][1]


def test_resume_loader_registered() -> None:
    assert resume_module.Resume._command_data.name == "resume"
    assert resume_module.Resume._command_data.description == t(
        "resume.command.description", locale="en_US"
    )
    assert resume_module.Resume._command_data.description == "Resume the paused track."
