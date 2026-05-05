"""Tests for ``ryzic.commands.replay``.

``_handle_play`` is the integration point we don't re-test here — its
own branches are covered exhaustively in ``tests/test_play_command.py``.
We patch it to a recording stub to assert the routing contract: replay
forwards the right URL to the existing /play body, after the history
lookup succeeds.
"""

from __future__ import annotations

from typing import Any, cast

import lightbulb
import pytest

from ryzic import track_history
from ryzic.commands import replay as replay_module
from tests._command_helpers import (
    FakeBot,
    context_for,
    make_track_info,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    track_history._reset_state_for_test()


@pytest.fixture
def play_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[lightbulb.Context, str]]:
    """Replace ``_handle_play`` with a recorder; return the captured calls."""
    calls: list[tuple[lightbulb.Context, str]] = []

    async def fake_handle_play(ctx: lightbulb.Context, url: str) -> None:
        calls.append((ctx, url))

    monkeypatch.setattr(replay_module, "_handle_play", fake_handle_play)
    return calls


async def test_outside_guild_short_circuits(
    play_calls: list[tuple[lightbulb.Context, str]],
) -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=None)

    await replay_module._handle_replay(ctx, position=1)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Run /replay in a server."
    assert fake.responses[0][1].get("ephemeral") is True
    assert play_calls == []


async def test_empty_history_returns_friendly_ephemeral(
    play_calls: list[tuple[lightbulb.Context, str]],
) -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)

    await replay_module._handle_replay(ctx, position=1)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "No tracks have played yet."
    assert play_calls == []


async def test_position_out_of_range_returns_friendly_ephemeral(
    play_calls: list[tuple[lightbulb.Context, str]],
) -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)
    track_history.record(111, make_track_info(title="Only"))

    await replay_module._handle_replay(ctx, position=5)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Only 1 track in history."
    assert fake.responses[0][1].get("ephemeral") is True
    assert play_calls == []


async def test_position_one_routes_through_handle_play_with_newest_url(
    play_calls: list[tuple[lightbulb.Context, str]],
) -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)
    track_history.record(111, make_track_info(video_id="aaaaaaaaaaa", url="https://x/a", title="A"))
    track_history.record(111, make_track_info(video_id="bbbbbbbbbbb", url="https://x/b", title="B"))

    await replay_module._handle_replay(ctx, position=1)

    # Position 1 = newest = "B".
    assert len(play_calls) == 1
    assert play_calls[0][1] == "https://x/b"
    # Pin the defer-once contract documented at replay.py:62-64 so a
    # future refactor that drops the defer (or moves it into
    # ``_handle_play`` and double-defers from ``Play.invoke``) is caught.
    fake = cast(Any, ctx)
    assert fake.defer_calls == 1


async def test_position_two_picks_older_entry(
    play_calls: list[tuple[lightbulb.Context, str]],
) -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)
    track_history.record(111, make_track_info(video_id="aaaaaaaaaaa", url="https://x/a", title="A"))
    track_history.record(111, make_track_info(video_id="bbbbbbbbbbb", url="https://x/b", title="B"))

    await replay_module._handle_replay(ctx, position=2)

    assert len(play_calls) == 1
    assert play_calls[0][1] == "https://x/a"


def test_replay_loader_registered() -> None:
    assert replay_module.Replay._command_data.name == "replay"
    assert replay_module.Replay._command_data.description.startswith("Re-queue")
