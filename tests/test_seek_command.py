"""Tests for ``ryzic.commands.seek``."""

from __future__ import annotations

from typing import Any, cast

import pytest

from ryzic import lavalink_glue
from ryzic.commands import seek as seek_module
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


def _player_with_track(
    *,
    duration_ms: int = 213_000,
    position_ms: int = 0,
) -> tuple[FakeLavalinkClient, Any]:
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(duration=duration_ms)
    player.position = position_ms
    return ll, player


async def test_voice_precondition_short_circuits() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await seek_module._handle_seek(ctx, "0:30")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await seek_module._handle_seek(ctx, "0:30")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await seek_module._handle_seek(ctx, "0:30")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert fake.responses[0][1].get("ephemeral") is True


async def test_player_idle_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = None

    await seek_module._handle_seek(ctx, "0:30")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert player.seek_calls == []


async def test_unknown_duration_rejects() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=0)

    await seek_module._handle_seek(ctx, "1:30")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("seek.error.live_or_unknown", locale="en_US")
    assert "duration is unknown" in fake.responses[0][0]
    assert fake.responses[0][1].get("ephemeral") is True
    assert player.seek_calls == []


async def test_unparseable_position_rejects() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track()

    await seek_module._handle_seek(ctx, "garbage")

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("seek.error.bad_position", locale="en_US")
    assert "Couldn't read that position" in fake.responses[0][0]
    assert fake.responses[0][1].get("ephemeral") is True
    assert player.seek_calls == []


async def test_absolute_seek_invokes_player() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=213_000, position_ms=10_000)

    await seek_module._handle_seek(ctx, "1:30")

    fake = cast(Any, ctx)
    assert player.seek_calls == [90_000]
    assert fake.responses[0][0] == t("seek.success.jumped", locale="en_US", position="1:30")
    assert fake.responses[0][0] == "Jumped to 1:30."
    assert "ephemeral" not in fake.responses[0][1]


async def test_relative_seek_adds_to_position() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=213_000, position_ms=60_000)

    await seek_module._handle_seek(ctx, "+30")

    assert player.seek_calls == [90_000]


async def test_relative_seek_backward_clamps_at_zero() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=213_000, position_ms=10_000)

    await seek_module._handle_seek(ctx, "-30")

    # 10s - 30s = -20s, clamped to 0.
    assert player.seek_calls == [0]


async def test_absolute_seek_clamps_above_duration() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=213_000)

    await seek_module._handle_seek(ctx, "10:00")

    # 600s requested vs 213s duration → clamped to track end.
    assert player.seek_calls == [213_000]


async def test_bare_seconds_treated_as_absolute() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    _, player = _player_with_track(duration_ms=213_000, position_ms=100_000)

    await seek_module._handle_seek(ctx, "45")

    # Bare 45 → 45s absolute, NOT a relative jump from current position.
    assert player.seek_calls == [45_000]


async def test_seek_while_paused_works() -> None:
    """Regression test for #143: /seek should work on paused tracks."""
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(duration=213_000)
    player.position = 60_000  # Currently at 1 minute
    player.paused = True  # Paused!

    await seek_module._handle_seek(ctx, "2:00")

    # Should seek to 2 minutes, NOT return "Nothing is playing"
    assert player.seek_calls == [120_000]
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("seek.success.jumped", locale="en_US", position="2:00")
    assert "ephemeral" not in fake.responses[0][1]


def test_seek_loader_registered() -> None:
    assert seek_module.Seek._command_data.name == "seek"
    assert seek_module.Seek._command_data.description == t(
        "seek.command.description", locale="en_US"
    )
    assert (
        seek_module.Seek._command_data.description
        == "Jump to a position in the current track (m:ss, +30, or -15)."
    )
