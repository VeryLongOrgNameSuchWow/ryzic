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
from ryzic.i18n import t
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
    ctx = context_for(bot, guild_id=None, command_name="queue")

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("voice.error.run_in_server", locale="en_US", command="queue")
    assert fake.responses[0][0] == "Run /queue in a server."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_lavalink_client_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.empty_and_nothing_playing", locale="en_US")
    assert fake.responses[0][0] == "Queue is empty and nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.empty_and_nothing_playing", locale="en_US")


async def test_player_idle_returns_empty_message() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = None

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.empty_and_nothing_playing", locale="en_US")


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
    assert fake.responses[0][0] == t("queue.error.empty_and_nothing_playing", locale="en_US")


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
    assert embed.title is not None and embed.title.startswith("Queue (1 track ")


async def test_default_response_is_ephemeral() -> None:
    """``/queue`` defaults to ``private=True`` (issue #148).

    Status-introspection commands answer the invoker, not the channel.
    """
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is True


async def test_private_false_makes_response_public() -> None:
    """``private=False`` opts back to a public response (issue #100)."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))

    await queue_module._handle_queue(ctx, private=False)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is False
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)


async def test_private_true_makes_response_ephemeral() -> None:
    """``private=True`` (also the default since #148) routes to ephemeral."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))

    await queue_module._handle_queue(ctx, private=True)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is True
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
        locale="en_US",
    )
    # 2 queued tracks; 180+120 = 300_000 ms = 5:00.
    assert embed.title == "Queue (2 tracks · 5:00)"


def test_embed_title_pluralizes_correctly_for_singular_track() -> None:
    """One-track queue renders ``Queue (1 track · ...)`` (no rogue plural)."""
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[(make_track_info(video_id="aaaaaaaaaaa", title="A", duration_ms=60_000), 222)],
        locale="en_US",
    )
    assert embed.title == "Queue (1 track · 1:00)"


def test_embed_now_hint_shows_progress_and_link() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now", duration_ms=180_000),
        now_playing_position_ms=60_000,
        paused=False,
        queue=[],
        locale="en_US",
    )
    body = embed.description or ""
    assert body.startswith("Now: [**Now**](https://www.youtube.com/watch?v=dQw4w9WgXcQ)")
    assert "1:00 / 3:00" in body
    assert "(paused)" not in body


def test_embed_now_hint_appends_paused() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now", duration_ms=180_000),
        now_playing_position_ms=60_000,
        paused=True,
        queue=[],
        locale="en_US",
    )
    body = embed.description or ""
    assert "1:00 / 3:00 (paused)" in body


def test_embed_description_is_only_the_now_hint_when_queue_empty() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[],
        locale="en_US",
    )
    body = embed.description or ""
    assert body.startswith("Now: ")
    # Empty queue → no list body and no blank-line separator.
    assert "\n\n" not in body


def test_embed_description_lists_queued_entries_with_requester_mention() -> None:
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=[
            (make_track_info(video_id="aaaaaaaaaaa", title="A", duration_ms=60_000), 222),
            (make_track_info(video_id="bbbbbbbbbbb", title="B", duration_ms=120_000), 333),
        ],
        locale="en_US",
    )
    body = embed.description or ""
    assert "1. [A](https://www.youtube.com/watch?v=aaaaaaaaaaa) — 1:00 (req. by <@222>)" in body
    assert "2. [B](https://www.youtube.com/watch?v=bbbbbbbbbbb) — 2:00 (req. by <@333>)" in body


def test_embed_description_page_one_lists_first_ten_entries() -> None:
    """Page 1 (default) renders entries 1..10 of the full queue."""
    queue = [
        (make_track_info(video_id=f"vid_{i:07d}", title=f"T{i}", duration_ms=60_000), 222)
        for i in range(15)
    ]
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=queue,
        page=1,
        total_pages=2,
        locale="en_US",
    )
    body = embed.description or ""
    # 1..10 visible, 11..15 deferred to page 2.
    assert "10. [T9]" in body
    assert "11. [T10]" not in body
    # Issue #99 explicitly removed the "… and N more" overflow line —
    # paging is the new affordance for "see beyond 10".
    assert "and 5 more" not in body
    # Title gains the page indicator only when total_pages > 1.
    assert "page 1/2" in (embed.title or "")


def test_embed_description_page_two_lists_continuation_with_global_indices() -> None:
    """Page 2 starts at "11." — global indexing (issue #99)."""
    queue = [
        (make_track_info(video_id=f"vid_{i:07d}", title=f"T{i}", duration_ms=60_000), 222)
        for i in range(15)
    ]
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=queue,
        page=2,
        total_pages=2,
        locale="en_US",
    )
    body = embed.description or ""
    assert "11. [T10]" in body
    assert "15. [T14]" in body
    # First-page entries do NOT appear on page 2.
    assert "1. [T0]" not in body
    assert "page 2/2" in (embed.title or "")


def test_embed_title_omits_page_suffix_for_single_page_queues() -> None:
    """Default ``total_pages=1`` (≤ 10 entries) renders the original title shape."""
    queue = [
        (make_track_info(video_id=f"vid_{i:07d}", title=f"T{i}", duration_ms=60_000), 222)
        for i in range(3)
    ]
    embed = ux.build_queue_embed(
        now_playing=make_track_info(title="Now"),
        now_playing_position_ms=0,
        paused=False,
        queue=queue,
        locale="en_US",
    )
    title = embed.title or ""
    assert "page" not in title
    assert "Queue (3 tracks" in title


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
        locale="en_US",
    )
    body = embed.description or ""
    assert "\\*\\*hostile\\*\\*" in body
    assert "**hostile**" not in body


def test_loader_registered_queue_command() -> None:
    assert queue_module.Queue._command_data.name == "queue"
    assert queue_module.Queue._command_data.description == t(
        "queue.command.description", locale="en_US"
    )
    assert queue_module.Queue._command_data.description == (
        "Show the current track and upcoming queue."
    )


# ---------------------------------------------------------------------------
# Paging command branches (issue #99)
# ---------------------------------------------------------------------------


async def _setup_player_with_queue(n: int) -> Any:
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = make_track_with_info(make_track_info(title="Now"))
    player.queue = [
        make_track_with_info(
            make_track_info(video_id=f"vid_{i:07d}", title=f"T{i}", duration_ms=60_000)
        )
        for i in range(n)
    ]
    return player


async def test_default_page_renders_first_ten_when_queue_overflows() -> None:
    """No ``page`` arg → page 1 default → entries 1..10."""
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(15)

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    embed: hikari.Embed = fake.responses[0][1]["embed"]
    description = embed.description or ""
    assert "1. [T0]" in description
    assert "10. [T9]" in description
    assert "11. [T10]" not in description
    assert "page 1/2" in (embed.title or "")


async def test_page_two_renders_continuation() -> None:
    """``page=2`` → entries 11..15 (with global indices)."""
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(15)

    await queue_module._handle_queue(ctx, page=2)

    fake = cast(Any, ctx)
    embed: hikari.Embed = fake.responses[0][1]["embed"]
    description = embed.description or ""
    assert "11. [T10]" in description
    assert "15. [T14]" in description
    assert "1. [T0]" not in description
    assert "page 2/2" in (embed.title or "")


async def test_out_of_range_page_returns_friendly_ephemeral() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(5)  # only fits on page 1

    await queue_module._handle_queue(ctx, page=2)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.too_few_pages", locale="en_US", count=1)
    assert fake.responses[0][0] == "Queue has only 1 page."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_out_of_range_pluralizes_correctly_for_multiple_pages() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(15)  # 2 pages

    await queue_module._handle_queue(ctx, page=99)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.too_few_pages", locale="en_US", count=2)
    assert fake.responses[0][0] == "Queue has only 2 pages."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_empty_queue_with_default_page_renders_now_playing_only() -> None:
    """Empty queue + page=1 (default) → embed description is the ``Now: …`` hint only."""
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(0)

    await queue_module._handle_queue(ctx)

    fake = cast(Any, ctx)
    embed: hikari.Embed = fake.responses[0][1]["embed"]
    body = embed.description or ""
    # Empty queue → description is exactly the ``Now: …`` hint with no
    # blank-line separator or queue-list body.
    assert body.startswith("Now: ")
    assert "\n\n" not in body
    # Page-suffix omitted because total_pages=1 for an empty queue.
    assert "page" not in (embed.title or "")


async def test_empty_queue_with_explicit_page_two_returns_out_of_range() -> None:
    """Even an empty queue rejects ``page=2`` rather than rendering blank — clear feedback."""
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(0)

    await queue_module._handle_queue(ctx, page=2)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("queue.error.too_few_pages", locale="en_US", count=1)
    assert fake.responses[0][0] == "Queue has only 1 page."


async def test_exact_page_boundary_renders_full_last_page() -> None:
    """20-track queue at QUEUE_PAGE_SIZE=10 → 2 pages, page 2 renders all 10 entries."""
    bot = FakeBot()
    ctx = context_for(bot)
    await _setup_player_with_queue(20)

    await queue_module._handle_queue(ctx, page=2)

    fake = cast(Any, ctx)
    embed: hikari.Embed = fake.responses[0][1]["embed"]
    description = embed.description or ""
    assert "11. [T10]" in description
    assert "20. [T19]" in description
    # No 21 — that's beyond the queue; renderer must not synthesise extra rows.
    assert "21." not in description
    assert "page 2/2" in (embed.title or "")
