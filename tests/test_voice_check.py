"""Tests for ``ryzic.voice_check.ensure_same_voice`` + ``check_player_or_respond``.

We mock the slim subset of ``hikari.GatewayBot`` + ``lightbulb.Context``
needed by the helper. The real classes carry too many slots to
construct in-process; the helper only touches ``cache.get_voice_state``,
``get_me``, ``ctx.guild_id``, ``ctx.user``, ``ctx.command_data.name``,
and ``ctx.respond``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import lightbulb

from ryzic import voice_check
from ryzic.i18n import t


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


@dataclass
class _FakeCommandData:
    name: str = "skip"


class _FakeInteraction:
    """Bare interaction so ``locale_for_ephemeral`` falls back to ``en_US``."""


class _FakeContext:
    def __init__(
        self,
        guild_id: int | None,
        user_id: int,
        bot_user_id: int | None,
        states: Mapping[tuple[int, int], _FakeVoiceState | None],
        command_name: str = "skip",
    ) -> None:
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.client = _FakeClient(_FakeApp(bot_user_id, states))
        self.command_data = _FakeCommandData(name=command_name)
        self.interaction = _FakeInteraction()
        self.responses: list[tuple[Any, dict[str, Any]]] = []

    async def respond(self, content: Any = None, **kwargs: Any) -> None:
        self.responses.append((content, kwargs))


def _ctx(**kwargs: Any) -> lightbulb.Context:
    return cast(lightbulb.Context, _FakeContext(**kwargs))


async def test_returns_none_in_dm_uses_shared_run_in_server_key() -> None:
    """The DM bail names the invoking command via the shared catalog key.

    Pre-PR-C this said "Run this in a server."; PR C consolidates onto
    "Run /%{command} in a server." with the command name resolved from
    ``ctx.command_data.name`` so all 5 per-command sites + voice_check
    share a single key.
    """
    ctx = _FakeContext(guild_id=None, user_id=1, bot_user_id=10, states={}, command_name="pause")
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == t("voice.error.run_in_server", locale="en_US", command="pause")
    assert ctx.responses[0][0] == "Run /pause in a server."
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_returns_none_when_bot_user_missing() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=None, states={})
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == t("voice.error.bot_starting", locale="en_US")
    assert "starting up" in str(ctx.responses[0][0])


async def test_returns_none_when_bot_not_in_voice() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    assert ctx.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")


async def test_returns_none_when_user_in_different_channel() -> None:
    states = {
        (111, 10): _FakeVoiceState(channel_id=999),  # bot in 999
        (111, 1): _FakeVoiceState(channel_id=888),  # user in 888
    }
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states=states)
    assert await voice_check.ensure_same_voice(cast(lightbulb.Context, ctx)) is None
    # Direction matters: the rejection mentions the BOT's channel.
    assert ctx.responses[0][0] == t("voice.error.join_my_channel", locale="en_US", channel_id=999)
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
    assert ctx.responses[0][0] == t("voice.error.bot_not_in_voice", locale="en_US")


# ---------------------------------------------------------------------------
# check_player_or_respond — three-way guard for pause/resume/seek/skip (#215).
# ---------------------------------------------------------------------------


@dataclass
class _FakeAudioTrack:
    """Minimal track stub: only ``current is not None`` matters to the helper."""


class _FakePlayer:
    """Player stub with the ``is_connected`` / ``current`` surface the helper reads.

    Mirrors real ``lavalink.DefaultPlayer``: ``is_connected`` is derived from
    ``channel_id is not None``.
    """

    def __init__(self, *, channel_id: int | None, current: _FakeAudioTrack | None) -> None:
        self.channel_id = channel_id
        self.current = current

    @property
    def is_connected(self) -> bool:
        return self.channel_id is not None


async def test_check_player_or_respond_nothing_playing_responds_and_returns_none() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    result = await voice_check.check_player_or_respond(cast(lightbulb.Context, ctx), player=None)
    assert result is None
    assert ctx.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert ctx.responses[0][0] == "Nothing is playing."
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_check_player_or_respond_no_current_responds_and_returns_none() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    player = _FakePlayer(channel_id=999, current=None)
    assert (
        await voice_check.check_player_or_respond(
            cast(lightbulb.Context, ctx), player=cast(Any, player)
        )
        is None
    )
    assert ctx.responses[0][0] == t("np.error.nothing_playing", locale="en_US")


async def test_check_player_or_respond_reconnecting_responds_and_returns_none() -> None:
    """#215: a held ``current`` with ``is_connected=False`` is the resync window.

    Reject with ``voice.error.reconnecting`` ephemeral — NOT the misleading
    "Nothing is playing." (a track IS held) and NOT a silent proceed.
    """
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    player = _FakePlayer(channel_id=None, current=_FakeAudioTrack())
    result = await voice_check.check_player_or_respond(
        cast(lightbulb.Context, ctx), player=cast(Any, player)
    )
    assert result is None
    assert ctx.responses[0][0] == t("voice.error.reconnecting", locale="en_US")
    assert ctx.responses[0][0] == "Reconnecting to voice — try again in a moment."
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_check_player_or_respond_ready_returns_player() -> None:
    ctx = _FakeContext(guild_id=111, user_id=1, bot_user_id=10, states={})
    player = _FakePlayer(channel_id=999, current=_FakeAudioTrack())
    result = await voice_check.check_player_or_respond(
        cast(lightbulb.Context, ctx), player=cast(Any, player)
    )
    assert result is player
    assert ctx.responses == []
