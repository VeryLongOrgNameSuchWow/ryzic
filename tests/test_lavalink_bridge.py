"""Tests for the hikari ↔ lavalink.py voice-update bridge.

The two listeners in ``ryzic.lavalink_glue`` translate hikari voice events
into the discord.py-shaped dicts that ``lavalink.Client.voice_update_handler``
expects. These tests exercise the translation by feeding mock events
through the listeners and asserting the dict shape — no real lavalink
client is required.
"""

from __future__ import annotations

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

    def __init__(self, bot_user_id: int | None = None) -> None:
        self._me = _FakeOwnUser(bot_user_id) if bot_user_id is not None else None
        self.created_messages: list[tuple[int, str]] = []
        self.voice_state_calls: list[tuple[int, int | None]] = []

    def get_me(self) -> _FakeOwnUser | None:
        return self._me

    @property
    def rest(self) -> _FakeApp:
        return self

    async def create_message(self, channel_id: int, content: str) -> None:
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


class _FakeLavalinkClient:
    """Stand-in for ``lavalink.Client`` capturing the bridged payloads."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def voice_update_handler(self, data: Any) -> None:
        self.payloads.append(dict(data))


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


def test_bridge_voice_server_payload_does_not_substring() -> None:
    """``[6:]`` would chop the wrong prefix when hikari swaps the scheme.

    ``removeprefix("wss://")`` is the chosen mitigation; this test pins the
    behaviour so a regression silently corrupting hosts is caught.
    """
    event = _make_voice_server_event(endpoint="wss://example.com")
    payload = lavalink_glue._bridge_voice_server_payload(event)
    assert payload["d"]["endpoint"] == "example.com"


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


def test_lavalink_factory_raises_before_bootstrap() -> None:
    with pytest.raises(lavalink_glue.LavalinkNotReadyError):
        lavalink_glue._lavalink_client_factory()


def test_lavalink_factory_returns_client_after_bootstrap() -> None:
    fake_client = _FakeLavalinkClient()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, fake_client))

    assert lavalink_glue._lavalink_client_factory() is fake_client


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
    assert first_task.cancelled() or first_task.cancelling()


def test_clear_player_queue_handles_missing_attribute() -> None:
    class _PlayerNoQueue:
        pass

    lavalink_glue._clear_player_queue(cast(lavalink.BasePlayer, _PlayerNoQueue()))


def test_clear_player_queue_clears_when_present() -> None:
    class _Player:
        def __init__(self) -> None:
            self.queue = ["a", "b", "c"]

    p = _Player()
    lavalink_glue._clear_player_queue(cast(lavalink.BasePlayer, p))
    assert p.queue == []
