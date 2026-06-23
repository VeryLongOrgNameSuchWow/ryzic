"""Tests for ``ryzic.commands.leave``."""

from __future__ import annotations

import asyncio
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


async def test_leave_calls_teardown_player_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/leave must go through ``_teardown_player_session`` (not duplicate steps)."""
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True

    calls: list[tuple[int, int]] = []
    real_teardown = lavalink_glue._teardown_player_session

    async def _spy_teardown(b: Any, guild_id: int, p: Any) -> None:
        calls.append((guild_id, id(p)))
        await real_teardown(b, guild_id, p)

    monkeypatch.setattr(lavalink_glue, "_teardown_player_session", _spy_teardown)
    await leave_module._handle_leave(ctx)

    assert len(calls) == 1
    assert calls[0][0] == 111
    assert calls[0][1] == id(cast(Any, player))


async def test_leave_cancels_pending_auto_leave_timer() -> None:
    """Regression for #218: /leave must cancel a pending auto-leave task."""
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True

    lavalink_glue._start_auto_leave(cast(Any, bot), 111)
    task = lavalink_glue.auto_leave_tasks[111]
    assert not task.done()

    await leave_module._handle_leave(ctx)

    assert 111 not in lavalink_glue.auto_leave_tasks
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_leave_cancels_auto_leave_task_past_its_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #218: a timer whose sleep completes during /leave's
    first await must not emit its "idle, disconnecting" broadcast.

    The cancel bundled inside ``_teardown_player_session`` runs only after
    ``player.stop()`` and ``update_voice_state``; a timer that wakes during
    one of those awaits self-pops before that late cancel and runs its body
    to completion. Cancelling up front — before the first await — closes the
    window. This test rigs the timer to wake at the first event-loop yield
    (inside ``player.stop()``) so the race is deterministic: without the
    up-front cancel the spurious broadcast fires and a second
    ``update_voice_state`` is recorded.
    """
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True

    lavalink_glue._set_auto_leave_seconds_for_test(300)

    real_sleep = asyncio.sleep

    async def _noop_sleep(_: float) -> None:
        return

    # Yield once inside player.stop() so the auto-leave task — whose sleep
    # is a no-op — gets loop time and can run its post-sleep body during
    # /leave's first await (the window the up-front cancel must close).
    async def _yielding_stop(self: Any) -> None:
        await real_sleep(0)
        self.stop_calls += 1
        self.current = None

    broadcasts: list[int] = []
    real_send = lavalink_glue._send_to_last_play_channel

    async def _spy_send(b: Any, guild_id: int, content: str) -> None:
        broadcasts.append(guild_id)
        await real_send(b, guild_id, content)

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(type(player), "stop", _yielding_stop)
    monkeypatch.setattr(lavalink_glue, "_send_to_last_play_channel", _spy_send)

    lavalink_glue._start_auto_leave(cast(Any, bot), 111)
    task = lavalink_glue.auto_leave_tasks[111]
    assert not task.done()

    await leave_module._handle_leave(ctx)

    # Timer cancelled before its body ran: no spurious broadcast, no second
    # update_voice_state from _auto_leave, task ended cancelled.
    assert broadcasts == []
    assert 111 not in lavalink_glue.auto_leave_tasks
    assert task.cancelled()
    assert bot.update_voice_state_calls == [(111, None)]


async def test_leave_suppresses_auto_leave_broadcast_when_timer_already_past_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the #218 dispatch-boundary edge: the timer's sleep
    completed in the same loop tick as /leave and the loop ran _auto_leave
    first, so it self-popped before /leave's handler ran — the dict-keyed
    _cancel_auto_leave cannot reach it. begin_explicit_leave is the backstop:
    _auto_leave bows out after its update_voice_state await instead of
    broadcasting "idle, disconnecting" alongside /leave's "left".

    The rig parks _auto_leave mid-body at update_voice_state (past its sleep,
    self-popped) before /leave runs, which is the exact state the up-front
    cancel alone cannot handle.
    """
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True

    lavalink_glue._set_auto_leave_seconds_for_test(300)

    real_sleep = asyncio.sleep

    async def _noop_sleep(_: float) -> None:
        return

    # Yield once inside update_voice_state so _auto_leave parks there (past
    # its no-op sleep, self-popped) and resumes only when /leave yields.
    async def _yielding_update_voice_state(
        self: Any, guild_id: int, channel_id: int | None, *, self_deaf: bool = False
    ) -> None:
        await real_sleep(0)
        self.update_voice_state_calls.append((guild_id, channel_id))

    broadcasts: list[int] = []
    real_send = lavalink_glue._send_to_last_play_channel

    async def _spy_send(b: Any, guild_id: int, content: str) -> None:
        broadcasts.append(guild_id)
        await real_send(b, guild_id, content)

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(type(bot), "update_voice_state", _yielding_update_voice_state)
    monkeypatch.setattr(lavalink_glue, "_send_to_last_play_channel", _spy_send)

    lavalink_glue._start_auto_leave(cast(Any, bot), 111)
    task = lavalink_glue.auto_leave_tasks[111]
    # Run _auto_leave to its update_voice_state await: past its (no-op) sleep,
    # self-popped, parked mid-body. This is the state the up-front cancel
    # alone cannot reach (the dict entry is already gone).
    await real_sleep(0)
    assert 111 not in lavalink_glue.auto_leave_tasks
    assert not task.done()

    await leave_module._handle_leave(ctx)

    # _auto_leave bowed out via the explicit-leave marker: no spurious
    # broadcast. /leave still owns the response.
    assert broadcasts == []
    assert not task.cancelled()
    assert task.done()
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("leave.success.left", locale="en_US")
    assert lavalink_glue._explicit_leave_in_progress == set()


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
