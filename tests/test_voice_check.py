"""Tests for ``ryzic.voice_check.ensure_same_voice``.

We mock the slim subset of ``hikari.GatewayBot`` + ``lightbulb.Context``
needed by the helper. The real classes carry too many slots to
construct in-process; the helper only touches ``cache.get_voice_state``,
``get_me``, ``ctx.guild_id``, ``ctx.user``, and ``ctx.respond``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import lightbulb

from ryzic import voice_check


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeVoiceState:
    channel_id: int | None


class _FakeCache:
    def __init__(self, states: Mapping[tuple[int, int], _FakeVoiceState | None]) -> None:
        self._states = states

    def get_voice_state(self, guild_id: int, user_id: int) -> _FakeVoiceState | None:
        return self._states.get((guild_id, user_id))


class _FakeApp:
    def __init__(
        self,
        bot_user_id: int | None,
        states: Mapping[tuple[int, int], _FakeVoiceState | None],
    ) -> None:
        self._me = _FakeUser(bot_user_id) if bot_user_id is not None else None
        self.cache = _FakeCache(states)

    def get_me(self) -> _FakeUser | None:
        return self._me


class _FakeClient:
    def __init__(self, app: _FakeApp) -> None:
        self.app = app


class _FakeContext:
    def __init__(
        self,
        guild_id: int | None,
        user_id: int,
        bot_user_id: int | None,
        states: Mapping[tuple[int, int], _FakeVoiceState | None],
    ) -> None:
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.client = _FakeClient(_FakeApp(bot_user_id, states))
        self.responses: list[tuple[Any, dict[str, Any]]] = []

    async def respond(self, content: Any = None, **kwargs: Any) -> None:
        self.responses.append((content, kwargs))


def _ctx(**kwargs: Any) -> lightbulb.Context:
    return cast(lightbulb.Context, _FakeContext(**kwargs))


async def test_returns_none_in_dm() -> None:
    ctx = _FakeContext(guild_id=None, user_id=1, bot_user_id=10, states={})
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == "Run this in a server."
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_returns_none_when_bot_user_missing() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=None, states={})
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert "starting up" in str(ctx.responses[0][0])


async def test_returns_none_when_bot_not_in_voice() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == "I'm not in a voice channel."


async def test_returns_none_when_user_in_different_channel() -> None:
    states = {
        (111, 10): _FakeVoiceState(channel_id=999),  # bot in 999
        (111, 1): _FakeVoiceState(channel_id=888),  # user in 888
    }
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states=states)
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    # Direction matters: the rejection mentions the BOT's channel.
    assert "<#999>" in str(ctx.responses[0][0])


async def test_returns_none_when_user_not_in_voice() -> None:
    states = {(111, 10): _FakeVoiceState(channel_id=999)}
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states=states)
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert "<#999>" in str(ctx.responses[0][0])


async def test_returns_channel_id_on_match() -> None:
    states = {
        (111, 10): _FakeVoiceState(channel_id=999),
        (111, 1): _FakeVoiceState(channel_id=999),
    }
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states=states)
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) == 999
    assert ctx.responses == []


async def test_bot_state_with_none_channel_treated_as_disconnected() -> None:
    states = {
        (111, 10): _FakeVoiceState(channel_id=None),
        (111, 1): _FakeVoiceState(channel_id=999),
    }
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states=states)
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == "I'm not in a voice channel."
