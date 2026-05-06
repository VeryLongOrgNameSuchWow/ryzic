"""Tests for embed builders + safe-formatting helpers."""

from __future__ import annotations

import hikari
import pytest

from ryzic import ux
from ryzic.ytdlp import PlaylistInfo, TrackInfo


def _track(**overrides: object) -> TrackInfo:
    base: dict[str, object] = {
        "video_id": "dQw4w9WgXcQ",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration_ms": 213_000,
    }
    base.update(overrides)
    return TrackInfo(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# escape_markdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("[link](url)", "\\[link\\]\\(url\\)"),
        ("**bold**", "\\*\\*bold\\*\\*"),
        ("`code`", "\\`code\\`"),
        ("under_score", "under\\_score"),
        ("~strike~", "\\~strike\\~"),
        ("|spoil|", "\\|spoil\\|"),
        ("> quote", "\\> quote"),
    ],
)
def test_escape_markdown(raw: str, expected: str) -> None:
    assert ux.escape_markdown(raw) == expected


def test_escape_markdown_handles_pre_existing_backslashes() -> None:
    # The escape pass adds ``\\`` to existing ``\``; subsequent chars
    # are not double-escaped because we replace ``\`` first.
    assert ux.escape_markdown("a\\b*c") == "a\\\\b\\*c"


# ---------------------------------------------------------------------------
# safe_truncate
# ---------------------------------------------------------------------------


def test_safe_truncate_no_op_under_limit() -> None:
    assert ux.safe_truncate("hi", 10) == "hi"


def test_safe_truncate_exact_limit_no_ellipsis() -> None:
    assert ux.safe_truncate("12345", 5) == "12345"


def test_safe_truncate_inserts_ellipsis_when_cut() -> None:
    assert ux.safe_truncate("123456789", 5) == "1234…"


def test_safe_truncate_handles_unicode_codepoint_safely() -> None:
    # Multi-byte but single-codepoint glyphs (emoji + accent).
    s = "🎵café"  # 5 codepoints
    assert ux.safe_truncate(s, 4) == "🎵ca…"


def test_safe_truncate_zero_max_returns_empty() -> None:
    assert ux.safe_truncate("hi", 0) == ""


def test_safe_truncate_negative_max_returns_empty() -> None:
    assert ux.safe_truncate("hi", -5) == ""


def test_safe_truncate_one_char_returns_ellipsis_only() -> None:
    assert ux.safe_truncate("hello", 1) == "…"


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (0, "0:00"),
        (1_000, "0:01"),
        (45_000, "0:45"),
        (60_000, "1:00"),
        (213_000, "3:33"),
        (3_600_000, "1:00:00"),
        (3_725_000, "1:02:05"),
    ],
)
def test_format_duration(ms: int, expected: str) -> None:
    assert ux.format_duration(ms) == expected


def test_format_duration_clamps_negative() -> None:
    assert ux.format_duration(-1234) == "0:00"


# ---------------------------------------------------------------------------
# parse_seek_position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0:00", (False, 0)),
        ("1:30", (False, 90_000)),
        ("12:34", (False, 754_000)),
        ("0:09", (False, 9_000)),
        # Single-digit seconds — Discord-mobile-friendly UX.
        ("1:5", (False, 65_000)),
        ("1:02:05", (False, 3_725_000)),
        ("0:00:30", (False, 30_000)),
        ("30", (False, 30_000)),
        ("0", (False, 0)),
        ("+30", (True, 30_000)),
        ("-15", (True, -15_000)),
        ("+0", (True, 0)),
        ("-0", (True, 0)),
        ("  1:30  ", (False, 90_000)),
    ],
)
def test_parse_seek_position_accepts_valid(raw: str, expected: tuple[bool, int]) -> None:
    assert ux.parse_seek_position(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "abc",
        "1:60",  # seconds field >= 60
        "1:99",  # seconds field >= 60
        # Symmetric: minutes >= 60 also rejected in H:MM:SS form.
        "0:60:30",
        "1:99:00",
        ":30",  # missing leading minutes
        "1:",  # missing seconds
        "+",  # sign without digits
        "-",  # sign without digits
        "+1:30",  # signed colon-form not supported
        "1.30",  # dot, not colon
        "1m30s",  # not the parser's grammar
        "30s",
        "--5",
        "+-5",
        "1:2:3:4",  # too many colons
    ],
)
def test_parse_seek_position_rejects_invalid(raw: str) -> None:
    assert ux.parse_seek_position(raw) is None


# ---------------------------------------------------------------------------
# build_queued_track_embed
# ---------------------------------------------------------------------------


def test_track_embed_playing_now() -> None:
    embed = ux.build_queued_track_embed(
        _track(),
        position=1,
        playing_now=True,
        channel_id=999,
        requester_id=222,
    )
    assert isinstance(embed, hikari.Embed)
    assert embed.title == "Queued"
    assert embed.description is not None
    assert "Never Gonna Give You Up" in embed.description
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in embed.description
    assert embed.footer is not None
    assert "Rick Astley" in (embed.footer.text or "")
    assert "playing now" in (embed.footer.text or "")
    assert "3:33" in (embed.footer.text or "")


def test_track_embed_position_in_queue() -> None:
    embed = ux.build_queued_track_embed(
        _track(),
        position=4,
        playing_now=False,
        channel_id=999,
        requester_id=222,
    )
    assert embed.footer is not None
    text = embed.footer.text or ""
    assert "position 4 in queue" in text


def test_track_embed_escapes_markdown_in_title_and_uploader() -> None:
    embed = ux.build_queued_track_embed(
        _track(title="evil **bold** [link](url)", uploader="hax_or"),
        position=1,
        playing_now=False,
        channel_id=999,
        requester_id=222,
    )
    assert embed.description is not None
    # Both ``**`` and ``[`` get escaped — neither bold nor a markdown link
    # can render in the embed body.
    assert "**bold**" not in embed.description
    assert "[link](url)" not in embed.description
    assert "\\*\\*bold\\*\\*" in embed.description
    assert embed.footer is not None
    assert "hax\\_or" in (embed.footer.text or "")


def test_track_embed_includes_channel_and_requester_fields() -> None:
    # Mention syntax (``<#…>`` / ``<@…>``) lives in inline embed fields
    # rather than the footer because Discord renders mentions as pills
    # in field values but as raw text in footers.
    embed = ux.build_queued_track_embed(
        _track(),
        position=1,
        playing_now=True,
        channel_id=999,
        requester_id=222,
    )
    assert embed.fields is not None
    field_pairs = {f.name: (f.value, f.is_inline) for f in embed.fields}
    assert field_pairs["Channel"] == ("<#999>", True)
    assert field_pairs["Requested by"] == ("<@222>", True)


# ---------------------------------------------------------------------------
# build_queued_playlist_embed
# ---------------------------------------------------------------------------


def _playlist(entries: list[TrackInfo] | None = None) -> PlaylistInfo:
    return PlaylistInfo(
        playlist_id="PLabcdefghij",
        title="My Playlist",
        entries=entries if entries is not None else [_track()],
    )


def test_playlist_embed_live_path() -> None:
    embed = ux.build_queued_playlist_embed(
        _playlist([_track(duration_ms=60_000), _track(duration_ms=180_000)]),
        requester="alice",
        used_cache=False,
        fetched_at=None,
        cache_is_stale=False,
    )
    assert embed.title == "Queued playlist"
    assert embed.description is not None
    assert "My Playlist" in embed.description
    assert "2 tracks" in embed.description
    # 60_000 + 180_000 = 240_000ms = 4:00
    assert "4:00" in embed.description
    assert embed.footer is not None
    assert "alice" in (embed.footer.text or "")
    # Regression guard: single-track embed (PR #75 / issue #69) added inline
    # fields for channel + requester. Playlist embed deliberately stays
    # field-free — keep the asymmetry explicit until/unless we harmonize.
    assert embed.fields == []


def test_playlist_embed_partial_failure_appends_footer_line() -> None:
    embed = ux.build_queued_playlist_embed(
        _playlist([_track(), _track()]),
        requester="alice",
        used_cache=False,
        fetched_at=None,
        cache_is_stale=False,
        failed_count=3,
    )
    assert embed.footer is not None
    assert "3 tracks could not be loaded" in (embed.footer.text or "")


def test_playlist_embed_no_partial_footer_when_zero_failed() -> None:
    embed = ux.build_queued_playlist_embed(
        _playlist(),
        requester="alice",
        used_cache=False,
        fetched_at=None,
        cache_is_stale=False,
        failed_count=0,
    )
    assert embed.footer is not None
    assert "could not be loaded" not in (embed.footer.text or "")


def test_playlist_embed_offline_metadata() -> None:
    embed = ux.build_queued_playlist_embed(
        _playlist(),
        requester="alice",
        used_cache=True,
        fetched_at=1234567890,
        cache_is_stale=False,
    )
    assert embed.title == "Queued playlist (offline metadata)"
    assert embed.footer is not None
    text = embed.footer.text or ""
    assert "yt-dlp could not refresh" in text
    assert "<t:1234567890:R>" in text
    assert "Tracks may fail individually" in text


def test_playlist_embed_offline_stale_warning() -> None:
    embed = ux.build_queued_playlist_embed(
        _playlist(),
        requester="alice",
        used_cache=True,
        fetched_at=1,
        cache_is_stale=True,
    )
    assert embed.footer is not None
    assert "snapshot is over 24h old" in (embed.footer.text or "")


def test_playlist_embed_escapes_markdown_in_title() -> None:
    embed = ux.build_queued_playlist_embed(
        PlaylistInfo(playlist_id="PLabcdefghij", title="**hax**", entries=[_track()]),
        requester="alice",
        used_cache=False,
        fetched_at=None,
        cache_is_stale=False,
    )
    assert embed.description is not None
    assert "**hax**" not in embed.description
    assert "\\*\\*hax\\*\\*" in embed.description


# ---------------------------------------------------------------------------
# build_recent_embed (issue #96)
# ---------------------------------------------------------------------------


def test_recent_embed_lists_tracks_in_order_and_counts() -> None:
    history = [
        _track(video_id="aaaaaaaaaaa", title="Newest"),
        _track(video_id="bbbbbbbbbbb", title="Older"),
    ]
    embed = ux.build_recent_embed(history)

    assert isinstance(embed, hikari.Embed)
    assert embed.title == "Recently played (2)"
    description = embed.description or ""
    assert "1. [Newest]" in description
    assert "2. [Older]" in description
    assert embed.footer is not None
    assert (embed.footer.text or "").startswith("Use /replay")


def test_recent_embed_escapes_markdown_in_titles() -> None:
    history = [_track(video_id="aaaaaaaaaaa", title="**evil** [link](http://x)")]
    embed = ux.build_recent_embed(history)

    assert embed.description is not None
    assert "**evil**" not in embed.description
    assert "\\*\\*evil\\*\\*" in embed.description
    assert "\\[link\\]" in embed.description


# ---------------------------------------------------------------------------
# build_now_playing_embed (issue #90)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("queue_length", "expected_label"),
    [
        (0, "empty"),
        (1, "1 track"),
        (2, "2 tracks"),
        (10, "10 tracks"),
    ],
)
def test_now_playing_embed_pluralizes_up_next_label(queue_length: int, expected_label: str) -> None:
    """The Up next field renders ``empty`` / ``N track`` / ``N tracks``."""
    embed = ux.build_now_playing_embed(
        _track(),
        position_ms=0,
        paused=False,
        queue_length=queue_length,
    )
    up_next_field = next(f for f in embed.fields if f.name == "Up next")
    assert up_next_field.value == expected_label
