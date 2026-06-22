"""Tests for the hikari ↔ lavalink.py voice-update bridge.

The two listeners in ``ryzic.lavalink_glue`` translate hikari voice events
into the discord.py-shaped dicts that ``lavalink.Client.voice_update_handler``
expects. These tests exercise the translation by feeding mock events
through the listeners and asserting the dict shape — no real lavalink
client is required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import hikari
import lavalink
import pytest
from dirty_equals import IsPartialDict

from ryzic import config, lavalink_glue
from ryzic.i18n import _broadcast_t
from tests._command_helpers import RecordingCache


@dataclass
class _FakeOwnUser:
    id: int


class _FakeShard:
    id = 0


class _FakeApp:
    """Minimal stand-in for ``hikari.GatewayBot`` used by the listeners."""

    def __init__(
        self,
        bot_user_id: int | None = None,
        create_message_error: Exception | None = None,
    ) -> None:
        self._me = _FakeOwnUser(bot_user_id) if bot_user_id is not None else None
        self.created_messages: list[tuple[int, str]] = []
        self.voice_state_calls: list[tuple[int, int | None]] = []
        self._create_message_error = create_message_error

    def get_me(self) -> _FakeOwnUser | None:
        return self._me

    @property
    def rest(self) -> _FakeApp:
        return self

    async def create_message(self, channel_id: int, content: str) -> None:
        if self._create_message_error is not None:
            raise self._create_message_error
        self.created_messages.append((channel_id, content))

    async def update_voice_state(self, guild_id: int, channel_id: int | None) -> None:
        self.voice_state_calls.append((guild_id, channel_id))


def _make_voice_server_event(
    guild_id: int = 111,
    endpoint: str | None = "wss://us-east1234.discord.media:443",
    token: str = "tok",
) -> hikari.VoiceServerUpdateEvent:
    """Build a hikari VoiceServerUpdateEvent without going through the gateway."""
    return hikari.VoiceServerUpdateEvent(
        app=cast(Any, _FakeApp()),
        shard=cast(Any, _FakeShard()),
        guild_id=hikari.Snowflake(guild_id),
        token=token,
        raw_endpoint=endpoint.removeprefix("wss://") if endpoint else None,
    )


def _make_voice_state_event(
    guild_id: int = 111,
    user_id: int = 222,
    channel_id: int | None = 333,
    session_id: str = "sess",
    bot_user_id: int | None = None,
) -> hikari.VoiceStateUpdateEvent:
    state = hikari.VoiceState(
        app=cast(Any, _FakeApp(bot_user_id=bot_user_id)),
        channel_id=hikari.Snowflake(channel_id) if channel_id is not None else None,
        guild_id=hikari.Snowflake(guild_id),
        is_guild_deafened=False,
        is_guild_muted=False,
        is_self_deafened=False,
        is_self_muted=False,
        is_streaming=False,
        is_suppressed=False,
        is_video_enabled=False,
        member=cast(Any, None),
        session_id=session_id,
        user_id=hikari.Snowflake(user_id),
        requested_to_speak_at=None,
    )
    return hikari.VoiceStateUpdateEvent(
        shard=cast(Any, _FakeShard()),
        old_state=None,
        state=state,
    )


def _make_guild_leave_event(guild_id: int = 111) -> hikari.GuildLeaveEvent:
    return hikari.GuildLeaveEvent(
        app=cast(Any, _FakeApp()),
        shard=cast(Any, _FakeShard()),
        guild_id=hikari.Snowflake(guild_id),
        old_guild=None,
    )


class _FakeLavalinkClient:
    """Stand-in for ``lavalink.Client`` capturing the bridged payloads."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def voice_update_handler(self, data: Any) -> None:
        self.payloads.append(dict(data))


class _FakeNotFoundError(hikari.NotFoundError):
    """Constructible stand-in for ``hikari.NotFoundError`` (which needs many kwargs)."""

    def __init__(self) -> None:  # pragma: no cover - trivial
        # Skip the parent __init__ entirely; tests only need isinstance checks.
        Exception.__init__(self, "not found")


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    lavalink_glue._set_lavalink_client_for_test(None)


def test_bridge_voice_server_payload_strips_wss_scheme() -> None:
    event = _make_voice_server_event(endpoint="wss://us-east1234.discord.media:443")
    payload = lavalink_glue._bridge_voice_server_payload(event)
    assert payload["t"] == "VOICE_SERVER_UPDATE"
    assert payload["d"] == IsPartialDict(
        endpoint="us-east1234.discord.media:443",
        guild_id="111",
        token="tok",
    )


def test_bridge_voice_server_payload_handles_none_endpoint() -> None:
    event = _make_voice_server_event(endpoint=None)
    payload = lavalink_glue._bridge_voice_server_payload(event)
    assert payload["d"]["endpoint"] is None


def test_bridge_voice_state_payload_shape() -> None:
    event = _make_voice_state_event(channel_id=999)
    payload = lavalink_glue._bridge_voice_state_payload(event)
    assert payload["t"] == "VOICE_STATE_UPDATE"
    assert payload["d"] == {
        "guild_id": "111",
        "user_id": "222",
        "channel_id": "999",
        "session_id": "sess",
    }


def test_bridge_voice_state_payload_null_channel() -> None:
    event = _make_voice_state_event(channel_id=None)
    payload = lavalink_glue._bridge_voice_state_payload(event)
    assert payload["d"]["channel_id"] is None


async def test_voice_server_listener_short_circuits_when_client_missing() -> None:
    fake_client = _FakeLavalinkClient()
    # _ll_client stays None → listener must NOT call into the lavalink client.
    event = _make_voice_server_event()
    await lavalink_glue._on_voice_server_update(event)
    assert fake_client.payloads == []


async def test_voice_state_listener_short_circuits_when_client_missing() -> None:
    """Symmetric guard test: the state listener must also no-op pre-bootstrap."""
    fake_client = _FakeLavalinkClient()
    # _ll_client stays None → listener must NOT call into the lavalink client.
    event = _make_voice_state_event()
    await lavalink_glue._on_voice_state_update(event)
    assert fake_client.payloads == []


async def test_voice_state_listener_tracks_own_user_even_when_client_missing() -> None:
    """Handshake bookkeeping must NOT share fate with the lavalink short-circuit.

    A residual VoiceStateUpdate for our own user during the bootstrap window
    still has to mark the guild ready, otherwise PR6a's first /play after a
    reconnect will time out.
    """
    # Park the waiter first; the setter must wake it up even with no
    # lavalink client installed.
    wait_task = asyncio.create_task(lavalink_glue.wait_for_voice_ready(111, timeout=1.0))
    await asyncio.sleep(0)
    event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(event)
    assert await wait_task is True


async def test_voice_server_listener_forwards_when_client_present() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_server_event()
    await lavalink_glue._on_voice_server_update(event)

    assert len(fake_client.payloads) == 1
    payload = fake_client.payloads[0]
    assert payload["t"] == "VOICE_SERVER_UPDATE"
    assert payload["d"] == IsPartialDict(
        guild_id="111",
        endpoint="us-east1234.discord.media:443",
    )


async def test_voice_server_listener_drops_non_discord_endpoint() -> None:
    """Defense in depth: only Discord voice endpoints get forwarded to lavalink."""
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_server_event(endpoint="wss://attacker.example:443")
    await lavalink_glue._on_voice_server_update(event)

    assert fake_client.payloads == []


async def test_voice_server_listener_drops_none_endpoint() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_server_event(endpoint=None)
    await lavalink_glue._on_voice_server_update(event)

    assert fake_client.payloads == []


async def test_voice_state_listener_forwards_payload() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_state_event()
    await lavalink_glue._on_voice_state_update(event)

    assert len(fake_client.payloads) == 1
    payload = fake_client.payloads[0]
    assert payload["t"] == "VOICE_STATE_UPDATE"
    assert payload["d"] == IsPartialDict(
        guild_id="111",
        user_id="222",
        channel_id="333",
        session_id="sess",
    )


async def test_voice_state_listener_sets_voice_ready_event_for_bot_user() -> None:
    """The handshake-race fix: when our own user joins, ``_voice_ready`` fires."""
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    # Park the waiter first (mirrors the real flow: ``/play`` calls
    # ``update_voice_state`` then immediately waits), then fire the
    # bot's own VOICE_STATE_UPDATE; the waiter must complete.
    wait_task = asyncio.create_task(lavalink_glue.wait_for_voice_ready(111, timeout=1.0))
    await asyncio.sleep(0)
    event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(event)
    assert await wait_task is True


async def test_voice_state_listener_does_not_set_event_for_other_users() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_state_event(user_id=99, bot_user_id=42)
    await lavalink_glue._on_voice_state_update(event)

    ok = await lavalink_glue.wait_for_voice_ready(111, timeout=0.05)
    assert ok is False


async def test_voice_state_listener_resets_event_when_bot_disconnects() -> None:
    """If the bot leaves, the per-guild event entry is removed."""
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    join_event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(join_event)
    assert 111 in lavalink_glue._voice_ready_events

    leave_event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=None)
    await lavalink_glue._on_voice_state_update(leave_event)
    assert 111 not in lavalink_glue._voice_ready_events


async def test_wait_for_voice_ready_returns_false_on_timeout() -> None:
    ok = await lavalink_glue.wait_for_voice_ready(123, timeout=0.05)
    assert ok is False


async def test_wait_for_voice_ready_resolves_when_join_arrives_after_wait_starts() -> None:
    """The actual race PR6a hits: /play starts waiting, THEN our own join lands.

    The previous shape (lazy ``_voice_ready_event(...)`` getter) and the
    current ``setdefault``-based shape both have to share the SAME Event
    between the waiter and the setter. Test pins it.
    """
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    wait_task = asyncio.create_task(lavalink_glue.wait_for_voice_ready(111, timeout=1.0))
    # Yield so the waiter parks on the Event before we set it.
    await asyncio.sleep(0)

    join_event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(join_event)

    assert await wait_task is True


async def test_wait_for_voice_ready_clears_stale_event_after_channel_move() -> None:
    """A non-disconnect move (A → B) leaves the prior event set; clear it.

    Regression for MEDIUM-2: ``_voice_ready_events`` is reset only on
    disconnect (channel_id None). A bot dragged to another channel by an
    admin or a /play after a /leave race must not return True instantly
    on a stale event — lavalink.py needs the new VOICE_SERVER_UPDATE
    before it can play to the new channel.
    """
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    # Synthesise the prior session: an event was created and set by an
    # earlier own-VoiceStateUpdate for channel A. The entry persists
    # until the bot disconnects (channel_id None), which has not
    # happened — a move from A to B leaves it set.
    stale = asyncio.Event()
    stale.set()
    lavalink_glue._voice_ready_events[111] = stale

    # The next ``wait_for_voice_ready`` must NOT return True instantly
    # on the stale event — it must block until the next own-state lands.
    assert await lavalink_glue.wait_for_voice_ready(111, timeout=0.05) is False

    # When the new own-state arrives, the waiter wakes up.
    wait_task = asyncio.create_task(lavalink_glue.wait_for_voice_ready(111, timeout=1.0))
    await asyncio.sleep(0)
    join_b = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=888)
    await lavalink_glue._on_voice_state_update(join_b)
    assert await wait_task is True


def test_get_lavalink_client_returns_none_before_bootstrap() -> None:
    assert lavalink_glue.get_lavalink_client() is None


def test_get_lavalink_client_returns_instance_after_bootstrap() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    assert lavalink_glue.get_lavalink_client() is fake_client


async def test_auto_leave_timer_cancellable() -> None:
    """``_cancel_auto_leave`` removes the entry and cancels the task."""
    bot = _FakeApp()
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    assert 111 in lavalink_glue.auto_leave_tasks

    lavalink_glue._cancel_auto_leave(111)
    assert 111 not in lavalink_glue.auto_leave_tasks


async def test_auto_leave_replaces_existing_timer() -> None:
    bot = _FakeApp()
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    first_task = lavalink_glue.auto_leave_tasks[111]

    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    second_task = lavalink_glue.auto_leave_tasks[111]

    assert first_task is not second_task
    # Drain the cancelled first task so it settles to ``cancelled() is True``
    # rather than ``cancelling()`` being truthy from the request alone.
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert first_task.cancelled()
    # Tidy up — leaving the second task in the loop leaks a 300s sleep.
    lavalink_glue._cancel_auto_leave(111)


async def test_auto_leave_seconds_zero_skips_scheduling() -> None:
    """``RYZIC_AUTOLEAVE_SECONDS=0`` (issue #62) must not arm a timer.

    A 0-second sleep would fire immediately and disconnect right after
    QueueEnd — the opposite of the operator's intent (24/7 ambient).
    """
    bot = _FakeApp()
    lavalink_glue._set_auto_leave_seconds_for_test(0)
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    assert 111 not in lavalink_glue.auto_leave_tasks


async def test_auto_leave_seconds_zero_still_cancels_existing_timer() -> None:
    """Mid-flight reconfiguration must not leave a stale 300s task armed.

    An operator who flips RYZIC_AUTOLEAVE_SECONDS=0 and restarts mid-queue
    is unlikely, but the contract holds: ``_start_auto_leave`` cancels
    first, then conditionally schedules.
    """
    bot = _FakeApp()
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    first_task = lavalink_glue.auto_leave_tasks[111]

    lavalink_glue._set_auto_leave_seconds_for_test(0)
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    assert 111 not in lavalink_glue.auto_leave_tasks

    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert first_task.cancelled()


async def test_auto_leave_seconds_default_uses_300_second_sleep() -> None:
    """Default wiring schedules with the 300s window."""
    bot = _FakeApp()
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    task = lavalink_glue.auto_leave_tasks[111]
    assert not task.done()
    # Tidy up so the 300s sleep doesn't outlive the test under pytest-randomly.
    lavalink_glue._cancel_auto_leave(111)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_auto_leave_seconds_custom_value_passed_into_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-default RYZIC_AUTOLEAVE_SECONDS value reaches ``_auto_leave``."""
    bot = _FakeApp()
    lavalink_glue._set_auto_leave_seconds_for_test(45)

    captured: list[int] = []
    real_sleep = asyncio.sleep

    async def _spy_sleep(seconds: int) -> None:
        captured.append(seconds)
        # Yield control immediately so the task progresses without actually
        # blocking 45 wall-clock seconds.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    task = lavalink_glue.auto_leave_tasks[111]
    await task
    assert captured == [45]


async def test_register_listeners_installs_auto_leave_seconds_from_cfg() -> None:
    """``register_listeners`` must propagate ``cfg.auto_leave_seconds`` into the module."""
    cfg = config.Config(
        discord_bot_token="x",
        lavalink_host="lavalink",
        lavalink_port=2333,
        lavalink_password="x",
        cache_dir=Path("/tmp"),
        cache_max_gb=5,
        log_level="INFO",
        guild_ids=(),
        auto_leave_seconds=42,
    )
    subscriptions: list[Any] = []

    class _Bot:
        def subscribe(self, event_type: Any, callback: Any) -> None:
            subscriptions.append((event_type, callback))

    lavalink_glue.register_listeners(cast(hikari.GatewayBot, _Bot()), cfg)
    assert lavalink_glue._auto_leave_seconds == 42
    # Sanity: the listener wiring still happens (4 subscriptions).
    assert len(subscriptions) == 4


async def test_queue_end_with_zero_seconds_logs_disabled_and_skips_timer() -> None:
    """The QueueEnd handler must respect the disabled-timer setting end-to-end."""
    bot = _FakeApp()
    lavalink_glue._set_auto_leave_seconds_for_test(0)
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    @dataclass
    class _Player:
        guild_id: int = 111

    @dataclass
    class _Event:
        player: _Player

    await handler.on_queue_end(cast(lavalink.QueueEndEvent, _Event(player=_Player())))
    assert 111 not in lavalink_glue.auto_leave_tasks


def test_format_idle_duration_renders_minutes_and_seconds() -> None:
    """User-facing message picks the natural unit."""
    assert lavalink_glue._format_idle_duration(300) == "5 minutes"
    assert lavalink_glue._format_idle_duration(120) == "2 minutes"
    assert lavalink_glue._format_idle_duration(60) == "1 minute"
    assert lavalink_glue._format_idle_duration(45) == "45 seconds"
    assert lavalink_glue._format_idle_duration(1) == "1 second"


async def test_send_to_last_play_channel_drops_stale_channel_on_not_found() -> None:
    """A deleted channel must not stay in ``last_play_channel`` forever."""
    bot = _FakeApp(create_message_error=_FakeNotFoundError())
    lavalink_glue.last_play_channel[111] = 999

    await lavalink_glue._send_to_last_play_channel(cast(hikari.GatewayBot, bot), 111, "x")

    assert 111 not in lavalink_glue.last_play_channel


async def test_send_to_last_play_channel_keeps_entry_on_other_hikari_errors() -> None:
    """Generic HikariError (e.g. transient 5xx) should NOT prune the mapping."""
    bot = _FakeApp(
        create_message_error=hikari.HikariError("transient")  # type: ignore[abstract]
    )
    lavalink_glue.last_play_channel[111] = 999

    await lavalink_glue._send_to_last_play_channel(cast(hikari.GatewayBot, bot), 111, "x")

    assert lavalink_glue.last_play_channel[111] == 999


async def test_guild_leave_clears_per_guild_state() -> None:
    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999
    lavalink_glue._voice_ready_events[111] = asyncio.Event()
    lavalink_glue._start_auto_leave(cast(hikari.GatewayBot, bot), 111)
    timer = lavalink_glue.auto_leave_tasks[111]

    await lavalink_glue._on_guild_leave(_make_guild_leave_event(111))

    assert 111 not in lavalink_glue.last_play_channel
    assert 111 not in lavalink_glue._voice_ready_events
    assert 111 not in lavalink_glue.auto_leave_tasks
    # The cancelled timer should settle.
    with pytest.raises(asyncio.CancelledError):
        await timer


async def test_track_exception_strips_markdown_and_caps_length() -> None:
    """MEDIUM-8: server-side error text must be sanitised before posting."""
    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999

    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    nasty = "`backticks` *bold* _italic_ |spoiler|\n# heading\n" + "x" * 500

    @dataclass
    class _Track:
        title: str = "ok title"

    @dataclass
    class _Player:
        guild_id: int = 111

    @dataclass
    class _Event:
        track: _Track
        player: _Player
        message: str
        cause: str
        severity: str = "FAULT"

    await handler.on_track_exception(
        cast(
            lavalink.TrackExceptionEvent,
            _Event(track=_Track(), player=_Player(), message=nasty, cause="should-not-appear"),
        )
    )

    assert len(bot.created_messages) == 1
    _, content = bot.created_messages[0]
    assert "`" not in content[content.index("failed:") :]
    assert "*bold*" not in content
    assert "_italic_" not in content
    assert "|spoiler|" not in content
    # Cause must NEVER leak even via fallback when message is set.
    assert "should-not-appear" not in content
    # Sanitised detail is capped at 200 chars; the surrounding template adds bytes,
    # but the detail substring itself must be bounded.
    detail_start = content.index("failed:") + len("failed: ")
    detail_end = content.rindex(". Skipping.")
    assert detail_end - detail_start <= 200
    # Catalog template envelope: ``Track **...** failed: ...  Skipping.``
    assert content.startswith("Track **ok title** failed: ")
    assert content.endswith(". Skipping.")


async def test_track_exception_falls_back_to_unknown_error_when_message_missing() -> None:
    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999

    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    @dataclass
    class _Track:
        title: str = "song"

    @dataclass
    class _Player:
        guild_id: int = 111

    @dataclass
    class _Event:
        track: _Track
        player: _Player
        message: str | None
        cause: str
        severity: str = "FAULT"

    await handler.on_track_exception(
        cast(
            lavalink.TrackExceptionEvent,
            _Event(
                track=_Track(),
                player=_Player(),
                message=None,
                cause="/opt/Lavalink/leak.txt secrets",
            ),
        )
    )

    assert len(bot.created_messages) == 1
    _, content = bot.created_messages[0]
    assert "unknown error" in content
    # Even the fallback path must not surface the JVM cause string.
    assert "/opt/Lavalink/leak.txt" not in content
    # Catalog rendering: ``_safe_error_text(None)`` → "unknown error" both
    # sides of "failed: ".
    assert content == _broadcast_t(
        "lavalink.broadcast.track_exception", title="song", detail="unknown error"
    )
    assert content == "Track **song** failed: unknown error. Skipping."


async def test_track_stuck_sanitises_title() -> None:
    """MEDIUM-8 also covers TrackStuck — a malicious title cannot inject markdown."""
    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999

    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    skipped: list[bool] = []

    class _Player:
        guild_id = 111

        async def skip(self) -> None:
            skipped.append(True)

    @dataclass
    class _Track:
        title: str = "*pwn*\n@everyone"

    @dataclass
    class _Event:
        track: _Track
        player: _Player
        threshold: int = 1000

    await handler.on_track_stuck(
        cast(lavalink.TrackStuckEvent, _Event(track=_Track(), player=_Player()))
    )

    assert skipped == [True]
    assert len(bot.created_messages) == 1
    _, content = bot.created_messages[0]
    # Title stripped of its own markdown chars; the **…** template wrap is
    # ours, not user-controlled. Catalog rendering and the literal must agree —
    # if either side drifts the byte-identical contract is broken.
    assert content == _broadcast_t("lavalink.broadcast.track_stuck", title="pwn")
    assert content == "Track **pwn** got stuck and was skipped."
    # The leading newline truncation drops the @everyone line entirely.
    assert "@everyone" not in content


def test_safe_error_text_handles_none_and_empty() -> None:
    assert lavalink_glue._safe_error_text(None) == "unknown error"
    assert lavalink_glue._safe_error_text("") == "unknown error"
    # Pure-markdown collapsing to empty also falls back.
    assert lavalink_glue._safe_error_text("`*_~|`") == "unknown error"


def test_is_valid_discord_endpoint_allowlist() -> None:
    assert lavalink_glue._is_valid_discord_endpoint("us-east1234.discord.media:443") is True
    assert lavalink_glue._is_valid_discord_endpoint("brazil.discord.media") is True
    assert lavalink_glue._is_valid_discord_endpoint("attacker.example:443") is False
    assert lavalink_glue._is_valid_discord_endpoint("evil.discord.media.attacker.com") is False
    assert lavalink_glue._is_valid_discord_endpoint(None) is False
    assert lavalink_glue._is_valid_discord_endpoint("") is False


# ---------------------------------------------------------------------------
# Audio cache release wiring (PR6a)
# ---------------------------------------------------------------------------


@dataclass
class _IdTrack:
    title: str = "song"
    identifier: str = "abc12345"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PlayerWithGuild:
    guild_id: int = 111


@dataclass
class _EndEvent:
    track: _IdTrack | None
    player: _PlayerWithGuild
    # ``lavalink.server.EndReason`` overrides ``__eq__`` without preserving
    # ``__hash__``, which trips dataclass's mutable-default guard. Wrap in
    # a factory so the enum value is sourced fresh per instance.
    reason: lavalink.server.EndReason = field(
        default_factory=lambda: lavalink.server.EndReason.FINISHED
    )


@dataclass
class _ExceptionEvent:
    track: _IdTrack | None
    player: _PlayerWithGuild
    message: str | None = "boom"
    cause: str = "<jvm trace>"
    severity: str = "FAULT"


async def test_track_end_releases_audio_cache_pin() -> None:
    """The TrackEnd hook MUST drop the audio cache refcount.

    ``LocalAudioSourceManager`` sets ``AudioTrack.identifier`` to the
    on-disk path; the release handler must recover the ``video_id``
    from the path stem so the per-video pin actually decrements.
    """
    from ryzic import audio_cache

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        path = "/var/cache/ryzic/audio/vi/vid67890.audio"
        await handler.on_track_end(
            cast(
                lavalink.TrackEndEvent,
                _EndEvent(track=_IdTrack(identifier=path), player=_PlayerWithGuild()),
            )
        )
        assert fake.released == ["vid67890"]
    finally:
        audio_cache.set_audio_cache(None)


async def test_track_exception_also_releases_audio_cache_pin() -> None:
    """LOAD_FAILED still has to release; otherwise the file pins forever."""
    from ryzic import audio_cache

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        lavalink_glue.last_play_channel[111] = 999
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        path = "/var/cache/ryzic/audio/fa/failed01.audio"
        await handler.on_track_exception(
            cast(
                lavalink.TrackExceptionEvent,
                _ExceptionEvent(track=_IdTrack(identifier=path), player=_PlayerWithGuild()),
            )
        )
        assert fake.released == ["failed01"]
    finally:
        audio_cache.set_audio_cache(None)


async def test_release_is_noop_without_cache_singleton() -> None:
    """Tests / startup ordering must tolerate a missing cache."""
    # No singleton installed; calling _release_track must not raise.
    await lavalink_glue._release_track(cast(lavalink.AudioTrack, _IdTrack()))


async def test_release_is_noop_for_none_track() -> None:
    """``TrackEnd`` carries Optional[AudioTrack]; ``None`` must short-circuit."""
    await lavalink_glue._release_track(None)


async def test_release_swallows_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    """A misbehaving cache must not take down the player loop."""
    from ryzic import audio_cache

    class _BoomCache:
        async def release(self, video_id: str) -> None:
            raise RuntimeError("kaboom")

    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, _BoomCache()))
    try:
        with caplog.at_level("ERROR", logger="ryzic.lavalink_glue"):
            await lavalink_glue._release_track(cast(lavalink.AudioTrack, _IdTrack()))
        assert any("failed to release" in r.message for r in caplog.records)
    finally:
        audio_cache.set_audio_cache(None)


async def test_release_with_path_identifier_decrements_real_cache_pin(
    tmp_path: Path,
) -> None:
    """Drive a real ``AudioCache`` to confirm path-stem extraction lands on the right key.

    Regression for HIGH-1: ``LocalAudioSourceManager`` populates
    ``AudioTrack.identifier`` with the on-disk path. Pinning happens by
    ``video_id`` (in ``get_or_download``); release must extract the
    same key from the path or eviction is permanently skipped.
    """
    from ryzic import audio_cache
    from ryzic.audio_cache import AudioCache

    cache = AudioCache(tmp_path, max_bytes=10_000_000)
    await cache.open()
    audio_cache.set_audio_cache(cache)
    try:
        video_id = "dQw4w9WgXcQ"
        # Simulate the ``get_or_download`` pin without doing the actual
        # download (we're testing release semantics, not download).
        cache._in_use[video_id] += 1
        assert cache._in_use[video_id] == 1

        # Path that ``LocalAudioSourceManager`` would surface as the
        # AudioTrack identifier for this cached file.
        path = str(tmp_path / "audio" / video_id[:2] / f"{video_id}.audio")
        await lavalink_glue._release_track(cast(lavalink.AudioTrack, _IdTrack(identifier=path)))

        # Counter must return to zero (key removed by ``release``).
        assert video_id not in cache._in_use
    finally:
        audio_cache.set_audio_cache(None)
        await cache.close()


# ---------------------------------------------------------------------------
# Queue-clear pin release (issue #24)
# ---------------------------------------------------------------------------


@dataclass
class _QueuePlayer:
    """``DefaultPlayer`` stand-in carrying just the surface clear_queue_releasing reads."""

    guild_id: int = 111
    queue: list[_IdTrack] = field(default_factory=list)


async def test_clear_queue_releasing_clears_then_releases_each_track() -> None:
    """The helper empties the queue first, then drops a pin per snapshotted track.

    Clear-then-release closes the race window where a concurrent ``/play``
    (i.e. ``player.queue.add``) lands between release ``await`` boundaries
    on the snapshot — see ``test_clear_queue_releasing_does_not_drop_concurrently_added_track``.
    """
    from ryzic import audio_cache

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        player = _QueuePlayer(
            queue=[
                _IdTrack(identifier="/var/cache/ryzic/audio/aa/aaaaaaaa.audio"),
                _IdTrack(identifier="/var/cache/ryzic/audio/bb/bbbbbbbb.audio"),
            ]
        )
        await lavalink_glue.clear_queue_releasing(cast(lavalink.DefaultPlayer, player))
        assert sorted(fake.released) == ["aaaaaaaa", "bbbbbbbb"]
        assert player.queue == []
    finally:
        audio_cache.set_audio_cache(None)


async def test_clear_queue_releasing_does_not_drop_concurrently_added_track() -> None:
    """Regression: a track ``/play`` adds during the release loop must survive.

    The previous shape (release-then-clear) walked the live queue and
    only emptied it after the last release ``await``. A concurrent
    ``player.queue.add()`` that landed between those awaits was wiped
    by the trailing ``queue.clear()`` without ever being released —
    the new pin leaked. Clear-then-release on a snapshot leaves any
    concurrently-added track in place.
    """
    from ryzic import audio_cache

    racing_track = _IdTrack(identifier="/var/cache/ryzic/audio/zz/zzracing.audio")

    class _AddOnReleaseCache:
        """Simulates ``/play`` queuing a new track while a release is in-flight."""

        def __init__(self, player: _QueuePlayer) -> None:
            self.player = player
            self.released: list[str] = []
            self._fired = False

        async def release(self, video_id: str) -> None:
            self.released.append(video_id)
            if not self._fired:
                self._fired = True
                # Mid-release: the racing producer's queue.add lands here.
                self.player.queue.append(racing_track)

    player = _QueuePlayer(
        queue=[
            _IdTrack(identifier="/var/cache/ryzic/audio/aa/aaaaaaaa.audio"),
            _IdTrack(identifier="/var/cache/ryzic/audio/bb/bbbbbbbb.audio"),
        ]
    )
    fake = _AddOnReleaseCache(player)
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        await lavalink_glue.clear_queue_releasing(cast(lavalink.DefaultPlayer, player))
        # Both original tracks released exactly once (snapshot, not live).
        assert sorted(fake.released) == ["aaaaaaaa", "bbbbbbbb"]
        # The racing track survived the clear and was NOT released
        # (it would have been a permanent pin leak under the old shape).
        assert player.queue == [racing_track]
    finally:
        audio_cache.set_audio_cache(None)


async def test_clear_queue_releasing_handles_empty_queue() -> None:
    """No tracks → no releases, queue stays empty, no exception."""
    from ryzic import audio_cache

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        player = _QueuePlayer(queue=[])
        await lavalink_glue.clear_queue_releasing(cast(lavalink.DefaultPlayer, player))
        assert fake.released == []
        assert player.queue == []
    finally:
        audio_cache.set_audio_cache(None)


async def test_clear_queue_releasing_skips_tracks_without_identifier() -> None:
    """Defensive: tracks with no identifier are dropped without releasing."""
    from ryzic import audio_cache

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        player = _QueuePlayer(
            queue=[
                _IdTrack(identifier=""),
                _IdTrack(identifier="/var/cache/ryzic/audio/cc/cccccccc.audio"),
            ]
        )
        await lavalink_glue.clear_queue_releasing(cast(lavalink.DefaultPlayer, player))
        assert fake.released == ["cccccccc"]
        assert player.queue == []
    finally:
        audio_cache.set_audio_cache(None)


async def test_websocket_closed_4014_releases_queued_pins() -> None:
    """Voice 4014 (kicked / channel deleted) clears the queue without playing it.

    Without the helper those queued tracks would leak their pins forever
    (issue #24).
    """
    from ryzic import audio_cache

    @dataclass
    class _WsClosedEvent:
        player: _QueuePlayer
        code: int = 4014
        reason: str = "Disconnected"
        by_remote: bool = True

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        player = _QueuePlayer(
            queue=[_IdTrack(identifier="/var/cache/ryzic/audio/wc/wsclosed.audio")]
        )
        await handler.on_websocket_closed(
            cast(lavalink.WebSocketClosedEvent, _WsClosedEvent(player=player))
        )
        assert fake.released == ["wsclosed"]
        assert player.queue == []
    finally:
        audio_cache.set_audio_cache(None)


async def test_websocket_closed_non_4014_does_not_touch_queue() -> None:
    """Other close codes are transient; the queue (and pins) must survive."""
    from ryzic import audio_cache

    @dataclass
    class _WsClosedEvent:
        player: _QueuePlayer
        code: int = 1006  # transient
        reason: str = "Abnormal Closure"
        by_remote: bool = True

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        track = _IdTrack(identifier="/var/cache/ryzic/audio/kk/keptkept.audio")
        player = _QueuePlayer(queue=[track])
        await handler.on_websocket_closed(
            cast(lavalink.WebSocketClosedEvent, _WsClosedEvent(player=player))
        )
        assert fake.released == []
        assert player.queue == [track]
    finally:
        audio_cache.set_audio_cache(None)


async def test_guild_leave_releases_queued_pins_then_destroys_player() -> None:
    """Bot kicked / leaving a guild must release queued pins before destroy.

    Sibling of issue #24 (PR #28): without releasing first, queued tracks
    that never fire ``TrackEndEvent`` keep their pins forever — a noisy
    guild that kicks the bot would leak the entire queue.
    """
    from ryzic import audio_cache

    destroy_calls: list[int] = []
    queued_player = _QueuePlayer(
        queue=[
            _IdTrack(identifier="/var/cache/ryzic/audio/gl/glleave1.audio"),
            _IdTrack(identifier="/var/cache/ryzic/audio/gl/glleave2.audio"),
        ]
    )

    class _PlayerManager:
        def get(self, guild_id: int) -> _QueuePlayer | None:
            return queued_player if guild_id == 111 else None

        async def destroy(self, guild_id: int) -> None:
            destroy_calls.append(guild_id)

    class _Client:
        def __init__(self) -> None:
            self.player_manager = _PlayerManager()

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, _Client()))
    try:
        await lavalink_glue._on_guild_leave(_make_guild_leave_event(111))
        assert sorted(fake.released) == ["glleave1", "glleave2"]
        assert queued_player.queue == []
        # Existing teardown must still run so the player_manager entry goes away.
        assert destroy_calls == [111]
    finally:
        audio_cache.set_audio_cache(None)


async def test_node_disconnected_releases_queued_pins_for_every_player() -> None:
    """All player queues get their pins released when the node drops."""
    from ryzic import audio_cache

    @dataclass
    class _Node:
        name: str = "ryzic-default"

    @dataclass
    class _NodeDisconnectedEvent:
        node: _Node
        code: int | None = 1006
        reason: str | None = "lost"

    class _PlayerManager:
        def __init__(self, players: list[_QueuePlayer]) -> None:
            self._players = players

        def values(self) -> list[_QueuePlayer]:
            return list(self._players)

    class _ClientWithPlayers:
        def __init__(self, players: list[_QueuePlayer]) -> None:
            self.player_manager = _PlayerManager(players)

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        players = [
            _QueuePlayer(
                guild_id=111,
                queue=[_IdTrack(identifier="/var/cache/ryzic/audio/g1/guild1aa.audio")],
            ),
            _QueuePlayer(
                guild_id=222,
                queue=[
                    _IdTrack(identifier="/var/cache/ryzic/audio/g2/guild2aa.audio"),
                    _IdTrack(identifier="/var/cache/ryzic/audio/g2/guild2bb.audio"),
                ],
            ),
        ]
        client = _ClientWithPlayers(players)
        lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, client))

        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        await handler.on_node_disconnected(
            cast(lavalink.NodeDisconnectedEvent, _NodeDisconnectedEvent(node=_Node())),
        )
        assert sorted(fake.released) == ["guild1aa", "guild2aa", "guild2bb"]
        assert all(p.queue == [] for p in players)
    finally:
        audio_cache.set_audio_cache(None)


# ---------------------------------------------------------------------------
# Track history (issue #96)
# ---------------------------------------------------------------------------


def _track_with_info(title: str, *, identifier: str = "abc12345") -> _IdTrack:
    """Build an _IdTrack with an attached TrackInfo (matches /play's wiring)."""
    from ryzic import ux
    from tests._command_helpers import make_track_info

    track = _IdTrack(identifier=identifier)
    info = make_track_info(title=title)
    ux.attach_track_info(cast(lavalink.AudioTrack, track), info)
    return track


async def test_track_end_records_history_on_finished() -> None:
    """A naturally-finishing track is appended to the per-guild history."""
    from ryzic import track_history

    track_history._reset_state_for_test()
    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    track = _track_with_info("Song A")

    await handler.on_track_end(
        cast(
            lavalink.TrackEndEvent,
            _EndEvent(
                track=track,
                player=_PlayerWithGuild(),
                reason=lavalink.server.EndReason.FINISHED,
            ),
        )
    )

    history = track_history.get(111)
    assert [t.title for t in history] == ["Song A"]


async def test_track_end_records_history_on_replaced() -> None:
    """``/skip`` lands as REPLACED — still counts as 'user heard most of it'."""
    from ryzic import track_history

    track_history._reset_state_for_test()
    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    track = _track_with_info("Skipped")

    await handler.on_track_end(
        cast(
            lavalink.TrackEndEvent,
            _EndEvent(
                track=track,
                player=_PlayerWithGuild(),
                reason=lavalink.server.EndReason.REPLACED,
            ),
        )
    )

    assert [t.title for t in track_history.get(111)] == ["Skipped"]


@pytest.mark.parametrize(
    "reason",
    [
        lavalink.server.EndReason.LOAD_FAILED,
        lavalink.server.EndReason.STOPPED,
        lavalink.server.EndReason.CLEANUP,
    ],
)
async def test_track_end_does_not_record_for_excluded_reasons(
    reason: lavalink.server.EndReason,
) -> None:
    """LOAD_FAILED / STOPPED / CLEANUP must NOT enter history."""
    from ryzic import track_history

    track_history._reset_state_for_test()
    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    track = _track_with_info("Never Counted")

    await handler.on_track_end(
        cast(
            lavalink.TrackEndEvent,
            _EndEvent(track=track, player=_PlayerWithGuild(), reason=reason),
        )
    )

    assert track_history.get(111) == []


async def test_track_end_without_metadata_does_not_record() -> None:
    """A track with no attached TrackInfo is dropped — no half-populated rows."""
    from ryzic import track_history

    track_history._reset_state_for_test()
    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    bare = _IdTrack(identifier="bare0001")  # no attach_track_info call

    await handler.on_track_end(
        cast(
            lavalink.TrackEndEvent,
            _EndEvent(
                track=bare,
                player=_PlayerWithGuild(),
                reason=lavalink.server.EndReason.FINISHED,
            ),
        )
    )

    assert track_history.get(111) == []


async def test_track_end_with_none_track_does_not_record() -> None:
    """``TrackEndEvent`` carries ``Optional[AudioTrack]``; ``None`` short-circuits."""
    from ryzic import track_history

    track_history._reset_state_for_test()
    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    await handler.on_track_end(
        cast(
            lavalink.TrackEndEvent,
            _EndEvent(
                track=None,
                player=_PlayerWithGuild(),
                reason=lavalink.server.EndReason.FINISHED,
            ),
        )
    )

    assert track_history.get(111) == []


# ---------------------------------------------------------------------------
# Catalog rendering for ``lavalink.broadcast.*`` (PR I)
#
# Every broadcast string migrated to the catalog gets a unit-level
# ``_broadcast_t`` render that asserts byte-identical output against the
# pre-PR literal. Pairs with the integration-level assertions inside the
# event-handler tests above (belt-and-suspenders against catalog drift).
# ---------------------------------------------------------------------------


def test_broadcast_t_renders_auto_leave_with_duration() -> None:
    rendered = _broadcast_t("lavalink.broadcast.auto_leave", duration="5 minutes")
    assert rendered == "Idle for 5 minutes — disconnecting."


def test_broadcast_t_renders_voice_lost_without_vars() -> None:
    assert _broadcast_t("lavalink.broadcast.voice_lost") == "Voice connection lost. Queue cleared."


def test_broadcast_t_renders_node_reconnecting_without_vars() -> None:
    """N14 UX drift: ASCII ``...`` here vs ``…`` elsewhere; pinned as-is."""
    assert (
        _broadcast_t("lavalink.broadcast.node_reconnecting")
        == "Audio service disconnected. Reconnecting..."
    )


def test_broadcast_t_renders_track_stuck_with_title() -> None:
    rendered = _broadcast_t("lavalink.broadcast.track_stuck", title="My Song")
    assert rendered == "Track **My Song** got stuck and was skipped."


def test_broadcast_t_renders_track_exception_with_title_and_detail() -> None:
    rendered = _broadcast_t("lavalink.broadcast.track_exception", title="My Song", detail="403")
    assert rendered == "Track **My Song** failed: 403. Skipping."


async def test_auto_leave_broadcast_uses_catalog_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-leave timer's broadcast renders through ``_broadcast_t``.

    Verifies the end-to-end path (``_auto_leave`` → ``_send_to_last_play_channel``)
    posts the catalog rendering byte-identically.
    """

    class _ConnectedPlayer:
        is_connected = True

    class _ConnectedPlayerManager:
        def get(self, guild_id: int) -> _ConnectedPlayer:
            return _ConnectedPlayer()

    class _ConnectedClient:
        player_manager = _ConnectedPlayerManager()

    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, _ConnectedClient()))

    async def _instant_sleep(_: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    await lavalink_glue._auto_leave(cast(hikari.GatewayBot, bot), 111, 45)

    assert bot.created_messages == [
        (999, _broadcast_t("lavalink.broadcast.auto_leave", duration="45 seconds")),
    ]
    assert bot.created_messages[0][1] == "Idle for 45 seconds — disconnecting."


async def test_websocket_closed_4014_broadcasts_voice_lost_from_catalog() -> None:
    """Voice 4014 path posts the catalog rendering, not a Python literal."""

    @dataclass
    class _WsClosedEvent:
        player: _QueuePlayer
        code: int = 4014
        reason: str = "Disconnected"
        by_remote: bool = True

    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    await handler.on_websocket_closed(
        cast(lavalink.WebSocketClosedEvent, _WsClosedEvent(player=_QueuePlayer())),
    )

    assert bot.created_messages == [(999, _broadcast_t("lavalink.broadcast.voice_lost"))]
    assert bot.created_messages[0][1] == "Voice connection lost. Queue cleared."


async def test_websocket_closed_4014_skips_broadcast_on_intentional_disconnect() -> None:
    """When disconnect is intentional (Leave button, /leave, auto-leave), skip voice_lost.

    Regression for #170: the Leave button and /leave were triggering "Voice
    connection lost" broadcasts alongside their intentional-leave messages.
    """
    from ryzic import audio_cache

    @dataclass
    class _WsClosedEvent:
        player: _QueuePlayer
        code: int = 4014
        reason: str = "Disconnected"
        by_remote: bool = True

    fake = RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        lavalink_glue.last_play_channel[111] = 999
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        player = _QueuePlayer(
            queue=[_IdTrack(identifier="/var/cache/ryzic/audio/int/intentional.audio")]
        )

        # Mark disconnect as intentional before the WebSocket close fires
        lavalink_glue._mark_intentional_disconnect(111)

        await handler.on_websocket_closed(
            cast(lavalink.WebSocketClosedEvent, _WsClosedEvent(player=player)),
        )

        # No voice_lost broadcast — the disconnect was intentional
        assert bot.created_messages == []
        # Queue still cleared (for auto-leave case; /leave already cleared it)
        assert fake.released == ["intentional"]
        assert player.queue == []
        # Marker cleared after processing
        assert 111 not in lavalink_glue._pending_intentional_disconnects
    finally:
        audio_cache.set_audio_cache(None)


async def test_intentional_disconnect_marker_lifecycle() -> None:
    """The marker can be set, checked, and cleared."""
    lavalink_glue._mark_intentional_disconnect(111)
    assert 111 in lavalink_glue._pending_intentional_disconnects

    lavalink_glue._clear_intentional_disconnect(111)
    assert 111 not in lavalink_glue._pending_intentional_disconnects

    # Clearing a non-existent marker is safe (idempotent)
    lavalink_glue._clear_intentional_disconnect(222)
    assert 222 not in lavalink_glue._pending_intentional_disconnects


async def test_websocket_closed_4014_clears_intentional_disconnect_marker() -> None:
    """The marker is cleared even if no broadcast happens."""

    @dataclass
    class _WsClosedEvent:
        player: _QueuePlayer
        code: int = 4014
        reason: str = "Disconnected"
        by_remote: bool = True

    bot = _FakeApp()
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    lavalink_glue._mark_intentional_disconnect(111)
    assert 111 in lavalink_glue._pending_intentional_disconnects

    await handler.on_websocket_closed(
        cast(lavalink.WebSocketClosedEvent, _WsClosedEvent(player=_QueuePlayer())),
    )

    # Marker cleared after processing
    assert 111 not in lavalink_glue._pending_intentional_disconnects


async def test_node_disconnected_broadcasts_node_reconnecting_from_catalog() -> None:
    """NodeDisconnected path posts the catalog rendering, once per guild."""

    @dataclass
    class _Node:
        name: str = "ryzic-default"

    @dataclass
    class _NodeDisconnectedEvent:
        node: _Node
        code: int | None = 1006
        reason: str | None = "lost"

    class _PlayerManager:
        def __init__(self, players: list[_QueuePlayer]) -> None:
            self._players = players

        def values(self) -> list[_QueuePlayer]:
            return list(self._players)

    class _ClientWithPlayers:
        def __init__(self, players: list[_QueuePlayer]) -> None:
            self.player_manager = _PlayerManager(players)

    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999
    lavalink_glue.last_play_channel[222] = 888
    client = _ClientWithPlayers(
        [_QueuePlayer(guild_id=111, queue=[]), _QueuePlayer(guild_id=222, queue=[])]
    )
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, client))

    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
    await handler.on_node_disconnected(
        cast(lavalink.NodeDisconnectedEvent, _NodeDisconnectedEvent(node=_Node())),
    )

    expected = _broadcast_t("lavalink.broadcast.node_reconnecting")
    assert bot.created_messages == [(999, expected), (888, expected)]
    assert expected == "Audio service disconnected. Reconnecting..."


async def test_track_exception_broadcast_uses_catalog_template() -> None:
    """Track-exception path renders through the catalog with escape+strip composition.

    Title runs through ``_safe_error_text`` (strip + 1st-line + cap) THEN
    ``escape_markdown`` (per the markdown-``%{var}`` contract for vars
    inside ``**...**``). Detail stays on ``_safe_error_text`` to preserve
    the pre-PR rendering exactly.
    """
    bot = _FakeApp()
    lavalink_glue.last_play_channel[111] = 999
    handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))

    @dataclass
    class _Track:
        title: str = "Plain Title"

    @dataclass
    class _Player:
        guild_id: int = 111

    @dataclass
    class _Event:
        track: _Track
        player: _Player
        message: str
        cause: str
        severity: str = "FAULT"

    await handler.on_track_exception(
        cast(
            lavalink.TrackExceptionEvent,
            _Event(track=_Track(), player=_Player(), message="403 Forbidden", cause="x"),
        )
    )

    expected = _broadcast_t(
        "lavalink.broadcast.track_exception", title="Plain Title", detail="403 Forbidden"
    )
    assert bot.created_messages == [(999, expected)]
    assert bot.created_messages[0][1] == "Track **Plain Title** failed: 403 Forbidden. Skipping."
