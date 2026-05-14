"""Tests for ``ryzic.commands.recent``."""

from __future__ import annotations

from typing import Any, cast

import hikari
import pytest

from ryzic import track_history
from ryzic.commands import recent as recent_module
from ryzic.i18n import t
from tests._command_helpers import (
    FakeBot,
    context_for,
    make_track_info,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    track_history._reset_state_for_test()


async def test_outside_guild_short_circuits() -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=None)

    await recent_module._handle_recent(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.run_in_server", locale="en_US", command="recent")
    assert fake.responses[0][0] == "Run /recent in a server."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_empty_history_returns_friendly_ephemeral() -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)

    await recent_module._handle_recent(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "No tracks have played yet."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_renders_history_embed_newest_first() -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=111)
    track_history.record(111, make_track_info(video_id="aaaaaaaaaaa", title="First"))
    track_history.record(111, make_track_info(video_id="bbbbbbbbbbb", title="Second"))

    await recent_module._handle_recent(ctx)

    fake = cast(Any, ctx)
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)
    # Newest first — "Second" precedes "First" in the description.
    description = embed.description or ""
    assert description.index("Second") < description.index("First")


def test_recent_loader_registered() -> None:
    assert recent_module.Recent._command_data.name == "recent"
    assert recent_module.Recent._command_data.description.startswith("Show")
