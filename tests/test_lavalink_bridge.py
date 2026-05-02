"""Tests for the hikari ↔ lavalink.py voice-update bridge.

The two listeners in ``ryzic.lavalink_glue`` translate hikari voice events
into the discord.py-shaped dicts that ``lavalink.Client.voice_update_handler``
expects. These tests exercise the translation by feeding mock events
through the listeners and asserting the dict shape — no real lavalink
client is required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import hikari
import lavalink
import pytest

from ryzic import lavalink_glue


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
    assert payload["d"]["endpoint"] == "us-east1234.discord.media:443"
    assert payload["d"]["guild_id"] == "111"
    assert payload["d"]["token"] == "tok"


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
    event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(event)
    assert await lavalink_glue.wait_for_voice_ready(111, timeout=0.05) is True


async def test_voice_server_listener_forwards_when_client_present() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_server_event()
    await lavalink_glue._on_voice_server_update(event)

    assert len(fake_client.payloads) == 1
    payload = fake_client.payloads[0]
    assert payload["t"] == "VOICE_SERVER_UPDATE"
    assert payload["d"]["guild_id"] == "111"
    assert payload["d"]["endpoint"] == "us-east1234.discord.media:443"


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
    assert payload["d"]["guild_id"] == "111"
    assert payload["d"]["user_id"] == "222"
    assert payload["d"]["channel_id"] == "333"
    assert payload["d"]["session_id"] == "sess"


async def test_voice_state_listener_sets_voice_ready_event_for_bot_user() -> None:
    """The handshake-race fix: when our own user joins, ``_voice_ready`` fires."""
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(event)

    # ``wait_for_voice_ready`` should now resolve immediately.
    ok = await lavalink_glue.wait_for_voice_ready(111, timeout=0.05)
    assert ok is True


async def test_voice_state_listener_does_not_set_event_for_other_users() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    event = _make_voice_state_event(user_id=99, bot_user_id=42)
    await lavalink_glue._on_voice_state_update(event)

    ok = await lavalink_glue.wait_for_voice_ready(111, timeout=0.05)
    assert ok is False


async def test_voice_state_listener_resets_event_when_bot_disconnects() -> None:
    """If the bot leaves, the next ``/play`` must wait again — clear the event."""
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    join_event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999)
    await lavalink_glue._on_voice_state_update(join_event)
    assert await lavalink_glue.wait_for_voice_ready(111, timeout=0.05) is True

    leave_event = _make_voice_state_event(user_id=42, bot_user_id=42, channel_id=None)
    await lavalink_glue._on_voice_state_update(leave_event)

    assert await lavalink_glue.wait_for_voice_ready(111, timeout=0.05) is False


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
    # ours, not user-controlled.
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


class _RecordingCache:
    """Captures release() calls so tests can assert the integration."""

    def __init__(self) -> None:
        self.released: list[str] = []

    async def release(self, video_id: str) -> None:
        self.released.append(video_id)


@dataclass
class _IdTrack:
    title: str = "song"
    identifier: str = "abc12345"


@dataclass
class _PlayerWithGuild:
    guild_id: int = 111


@dataclass
class _EndEvent:
    track: _IdTrack | None
    player: _PlayerWithGuild
    reason: str = "FINISHED"


@dataclass
class _ExceptionEvent:
    track: _IdTrack | None
    player: _PlayerWithGuild
    message: str | None = "boom"
    cause: str = "<jvm trace>"
    severity: str = "FAULT"


async def test_track_end_releases_audio_cache_pin() -> None:
    """The TrackEnd hook MUST drop the audio cache refcount."""
    from ryzic import audio_cache

    fake = _RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        await handler.on_track_end(
            cast(
                lavalink.TrackEndEvent,
                _EndEvent(track=_IdTrack(identifier="vid67890"), player=_PlayerWithGuild()),
            )
        )
        assert fake.released == ["vid67890"]
    finally:
        audio_cache.set_audio_cache(None)


async def test_track_exception_also_releases_audio_cache_pin() -> None:
    """LOAD_FAILED still has to release; otherwise the file pins forever."""
    from ryzic import audio_cache

    fake = _RecordingCache()
    audio_cache.set_audio_cache(cast(audio_cache.AudioCache, fake))
    try:
        bot = _FakeApp()
        lavalink_glue.last_play_channel[111] = 999
        handler = lavalink_glue.EventHandler(cast(hikari.GatewayBot, bot))
        await handler.on_track_exception(
            cast(
                lavalink.TrackExceptionEvent,
                _ExceptionEvent(track=_IdTrack(identifier="failed01"), player=_PlayerWithGuild()),
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
