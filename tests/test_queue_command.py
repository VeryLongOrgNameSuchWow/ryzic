"""Tests for ``ryzic.commands.queue``.

The embed builder lives in ``ux.build_queue_embed`` — string formatting
edge cases (overflow, paused suffix, escaping) are exercised directly
against the builder so the command tests can stay focused on the
DM/missing-client/missing-player branches.
"""

from __future__ import annotations

from typing import Any, cast

import hikari
import pytest

from ryzic import lavalink_glue, ux
from ryzic.commands import queue as queue_module
from tests._command_helpers import (
    FakeAudioTrack,
    FakeBot,
    FakeLavalinkClient,
    context_for,
    install_lavalink_client,
    make_track_info,
    make_track_with_info,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    install_lavalink_client(None)


# ---------------------------------------------------------------------------
# Command-level branches (DM, no-lavalink, no-player, no-current)
# ---------------------------------------------------------------------------


async def test_dm_invocation_returns_friendly_error() -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=None)

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Run /queue in a server."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_lavalink_client_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Queue is empty and nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Queue is empty and nothing is playing."


async def test_player_idle_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = None

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Queue is empty and nothing is playing."


async def test_current_without_metadata_returns_empty_message() -> None:
    """Defensive: a future code path enqueueing without ``attach_track_info``."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = FakeAudioTrack()  # no track_info attached

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Queue is empty and nothing is playing."


# ---------------------------------------------------------------------------
# Happy paths via the command (embed plumbing only — content tested below)
# ---------------------------------------------------------------------------


async def test_command_responds_with_embed_when_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))
    player.position = 60_000
    player.queue = [make_track_with_info(make_track_info(video_id="aaaaaaaaaaa", title="Next"))]

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)
    assert embed.title is not None and embed.title.startswith("Queue (1 tracks")


async def test_default_response_is_public() -> None:
    """``/queue`` without ``private:True`` posts the embed to the channel."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    # Single response, public (ephemeral=False). Discord-side this means
    # the embed shows in the channel for everyone.
    assert fake.responses[0][1].get("ephemeral") is False


async def test_private_true_makes_response_ephemeral() -> None:
    """``private=True`` routes the embed to an ephemeral response (issue #100)."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))

    await queue_module._handle_queue(ctx, private=True)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is True
    # Embed itself is unchanged — same builder, same content.
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)


async def test_queued_tracks_without_metadata_are_silently_skipped() -> None:
    """A bare AudioTrack in the queue (no metadata) is dropped from the listing."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))
    player.queue = [
        make_track_with_info(make_track_info(video_id="aaaaaaaaaaa", title="Has-info")),
        FakeAudioTrack(title="No-info"),
    ]

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    embed = fake.responses[0][1]["embed"]
    assert embed.description is not None
    assert "Has-info" in embed.description
    assert "No-info" not in embed.description


# ---------------------------------------------------------------------------
# build_queue_embed: format of title, Now playing field, description body
# ---------------------------------------------------------------------------


def test_embed_title_counts_only_queued_tracks() -> None:
    """The title reports the queued count + queued total — not now-playing."""
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now", duration_ms=300_000),
        now_playing_position_ms=60_000,
        paused=False,
        queue=[
            (make_track_info(video_id="aaaaaaaaaaa", title="A", duration_ms=180_000), 222),
            (make_track_info(video_id="bbbbbbbbbbb", title="B", duration_ms=120_000), 333),
        ],
    )
    # 2 queued tracks; 180+120 = 300_000 ms = 5:00.
    assert embed.title == "Queue (2 tracks · 5:00)"


def test_embed_now_playing_field_shows_progress_and_link() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now", duration_ms=180_000),
        now_playing_position_ms=60_000,
        paused=False,
        queue=[],
    )
    field = embed.fields[0]
    assert field.name == "Now playing"
    assert "[**Now**](https://www.youtube.com/watch?v=dQw4w9WgXcQ)" in field.value
    assert "1:00 / 3:00" in field.value
    assert "(paused)" not in field.value


def test_embed_now_playing_field_appends_paused() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now", duration_ms=180_000),
        now_playing_position_ms=60_000,
        paused=True,
        queue=[],
    )
    field = embed.fields[0]
    assert "1:00 / 3:00 (paused)" in field.value


def test_embed_description_is_empty_when_queue_empty() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[],
    )
    assert embed.description in (None, "")


def test_embed_description_lists_queued_entries_with_requester_mention() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[
            (make_track_info(video_id="aaaaaaaaaaa", title="A", duration_ms=60_000), 222),
            (make_track_info(video_id="bbbbbbbbbbb", title="B", duration_ms=120_000), 333),
        ],
    )
    body = embed.description or ""
    assert "1. [A](https://www.youtube.com/watch?v=aaaaaaaaaaa) — 1:00 (req. by <@222>)" in body
    assert "2. [B](https://www.youtube.com/watch?v=bbbbbbbbbbb) — 2:00 (req. by <@333>)" in body


def test_embed_description_collapses_overflow_beyond_ten() -> None:
    """Beyond the first 10 entries the description appends an "… and N more" line."""
    queue = [
        (make_track_info(video_id=f"vid_{i:07d}", title=f"T{i}", duration_ms=60_000), 222)
        for i in range(15)
    ]
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=queue,
    )
    body = embed.description or ""
    # 1..10 listed, 11..15 collapsed.
    assert "10. [T9]" in body
    assert "11. [T10]" not in body
    assert "… and 5 more" in body


def test_embed_description_escapes_markdown_in_titles() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[
            (
                make_track_info(video_id="aaaaaaaaaaa", title="**hostile**", duration_ms=60_000),
                222,
            ),
        ],
    )
    body = embed.description or ""
    assert "\\*\\*hostile\\*\\*" in body
    assert "**hostile**" not in body


def test_loader_registered_queue_command() -> None:
    assert queue_module.Queue._command_data.name == "queue"
