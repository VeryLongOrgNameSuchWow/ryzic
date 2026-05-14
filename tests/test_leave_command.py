"""Tests for ``ryzic.commands.leave``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from ryzic import audio_cache, lavalink_glue
from ryzic.commands import leave as leave_module
from ryzic.i18n import t
from tests._command_helpers import (
    FakeAudioTrack,
    FakeBot,
    FakeLavalinkClient,
    RecordingCache,
    both_in_voice,
    context_for,
    install_lavalink_client,
)


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    lavalink_glue._reset_state_for_test()
    install_lavalink_client(None)
    audio_cache.set_audio_cache(None)
    yield
    audio_cache.set_audio_cache(None)


async def test_voice_precondition_short_circuits() -> None:
    bot = FakeBot()  # bot not in voice
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    # Comes from ``ensure_same_voice`` (voice_check.py), not leave.py.
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert bot.update_voice_state_calls == []


async def test_no_lavalink_client_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert bot.update_voice_state_calls == []


async def test_player_disconnected_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = False

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert player.stop_calls == 0


async def test_leave_stops_clears_disconnects_and_responds() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(title="Now Playing")
    player.queue = [FakeAudioTrack(title="A"), FakeAudioTrack(title="B")]

    await leave_module._handle_leave(ctx)

    assert player.stop_calls == 1
    assert player.queue == []
    assert bot.update_voice_state_calls == [(111, None)]
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("leave.success.left", locale="en_US")
    assert fake.responses[0][0] == "Left voice channel. Queue cleared."
    assert "ephemeral" not in fake.responses[0][1]


async def test_leave_releases_audio_cache_pins_for_queued_tracks() -> None:
    """Issue #24: queued-but-never-played tracks must release their cache pins.

    ``TrackEndEvent`` fires only for the currently-playing track on
    ``player.stop()``; queued tracks would otherwise sit in
    ``audio_cache._in_use`` forever, permanently disabling LRU eviction
    for those files.
    """
    fake_cache = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake_cache))
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(
        title="Now Playing",
        identifier="/var/cache/ryzic/audio/np/nowplay1.audio",
    )
    player.queue = [
        FakeAudioTrack(title="A", identifier="/var/cache/ryzic/audio/qa/queueda1.audio"),
        FakeAudioTrack(title="B", identifier="/var/cache/ryzic/audio/qb/queuedb2.audio"),
    ]

    await leave_module._handle_leave(ctx)

    assert player.queue == []
    # Both queued tracks released; the currently-playing track is released
    # by the TrackEndEvent path (a different code path; see _release_track
    # in lavalink_glue), not by clear_queue_releasing.
    assert sorted(fake_cache.released) == ["queueda1", "queuedb2"]


def test_leave_loader_registered() -> None:
    assert leave_module.Leave._command_data.name == "leave"
    assert leave_module.Leave._command_data.description == t(
        "leave.command.description", locale="en_US"
    )
    assert (
        leave_module.Leave._command_data.description == "Disconnect from voice and clear the queue."
    )
