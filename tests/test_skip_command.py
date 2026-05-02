"""Tests for ``ryzic.commands.skip``.

The voice-precondition check (``ensure_same_voice``) is exercised here
to confirm wiring; its own branches are covered exhaustively in
``tests/test_voice_check.py`` (don't re-test them).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from ryzic import lavalink_glue, ux
from ryzic.commands import skip as skip_module
from ryzic.ytdlp import TrackInfo
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


def _track_info(title: str = "Test Song") -> TrackInfo:
    return TrackInfo(
        video_id="dQw4w9WgXcQ",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title=title,
        uploader="Tester",
        duration_ms=180_000,
    )


def _track_with_info(title: str = "Test Song") -> FakeAudioTrack:
    track = FakeAudioTrack(title=title)
    ux.attach_track_info(cast(Any, track), _track_info(title=title))
    return track


async def test_voice_precondition_short_circuits() -> None:
    bot = FakeBot()  # bot not in voice → ensure_same_voice rejects
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "I'm not in a voice channel."
    # No player methods touched: short-circuit before lavalink lookup.
    assert ll.player_manager.players == {}


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."


async def test_player_idle_returns_nothing_playing() -> None:
    """Player exists but nothing is playing → nothing-playing message."""
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = None

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."
    assert player.skip_calls == 0


async def test_skip_with_more_in_queue_omits_empty_suffix() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = _track_with_info("Now")
    player.queue = [_track_with_info("Next")]

    await skip_module._handle_skip(ctx)

    assert player.skip_calls == 1
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Skipped **Now**."
    # Public response → no ephemeral kwarg.
    assert "ephemeral" not in fake.responses[0][1]


async def test_skip_last_track_appends_queue_empty() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = _track_with_info("Last")
    player.queue = []

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Skipped **Last**. Queue is empty."


async def test_skip_escapes_markdown_in_title() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = _track_with_info("**Hostile** [link](http://x)")
    player.queue = [_track_with_info("Next")]

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    body = str(fake.responses[0][0])
    # Markdown control chars are backslash-escaped, not stripped.
    assert "\\*\\*Hostile\\*\\*" in body
    assert "\\[link\\]" in body


async def test_skip_falls_back_to_audio_track_title_without_metadata() -> None:
    """If ``attach_track_info`` was bypassed, fall back to the AudioTrack title."""
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    raw = FakeAudioTrack(title="Bare AT Title")
    player.current = raw
    player.queue = []

    await skip_module._handle_skip(ctx)

    fake = cast(Any, ctx)
    assert "Bare AT Title" in str(fake.responses[0][0])


def test_skip_loader_registered() -> None:
    assert skip_module.Skip._command_data.name == "skip"
    assert skip_module.Skip._command_data.description.startswith("Skip")
