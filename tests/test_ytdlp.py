"""yt-dlp wrapper tests (M1 §6).

yt-dlp itself is mocked — these tests do not hit the network. We
exercise the async wrapper, error-mapping, livestream rejection,
video_id validation, and the frozen ``YoutubeDL`` options dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from yt_dlp.utils import DownloadError

from ryzic import ytdlp
from ryzic.errors import FetchFailed, InvalidVideoID

YTID = "dQw4w9WgXcQ"
TRACK_URL = f"https://www.youtube.com/watch?v={YTID}"
PLIST_URL = "https://www.youtube.com/playlist?list=PLabcdefghij"


def _track_info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": YTID,
        "webpage_url": TRACK_URL,
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration": 213,
        "is_live": False,
        "live_status": "not_live",
    }
    base.update(overrides)
    return base


def _playlist_info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "PLabcdefghij",
        "title": "Test playlist",
        "entries": [
            {"id": "abcdefghijk", "title": "Track A", "uploader": "User", "duration": 100},
            {"id": "lmnopqrstuv", "title": "Track B", "uploader": "User", "duration": 200},
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# validate_video_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "video_id",
    [
        "dQw4w9WgXcQ",  # canonical 11-char YouTube ID
        "abcdef",  # 6-char min
        "a" * 20,  # 20-char max
        "ABC_DEF-123",
    ],
)
def test_validate_video_id_accepts_valid(video_id: str) -> None:
    ytdlp.validate_video_id(video_id)  # does not raise


@pytest.mark.parametrize(
    "video_id",
    [
        "",
        "abc",  # too short
        "a" * 21,  # too long
        "abc/../etc",  # path traversal attempt
        "abc def",  # space
        "abc.def",  # dot
        "abc%20def",  # url-encoded
        "abc?def",  # query-string fragment
        "../../etc/passwd",
    ],
)
def test_validate_video_id_rejects_invalid(video_id: str) -> None:
    with pytest.raises(InvalidVideoID):
        ytdlp.validate_video_id(video_id)


# ---------------------------------------------------------------------------
# resolve_track
# ---------------------------------------------------------------------------


async def test_resolve_track_returns_track_info(tmp_path: Path) -> None:
    info = _track_info()
    with patch.object(ytdlp, "_sync_extract", return_value=info) as m:
        track = await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
    assert track == ytdlp.TrackInfo(
        video_id=YTID,
        url=TRACK_URL,
        title="Never Gonna Give You Up",
        uploader="Rick Astley",
        duration_ms=213_000,
    )
    # Single round-trip, no download.
    assert m.call_count == 1
    _opts, called_url = m.call_args.args
    assert called_url == TRACK_URL
    assert m.call_args.kwargs == {"download": False}


async def test_resolve_track_rejects_active_livestream(tmp_path: Path) -> None:
    info = _track_info(is_live=True, live_status="is_live")
    with (
        patch.object(ytdlp, "_sync_extract", return_value=info),
        pytest.raises(FetchFailed, match="livestream"),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


async def test_resolve_track_rejects_upcoming_livestream(tmp_path: Path) -> None:
    info = _track_info(is_live=False, live_status="is_upcoming")
    with (
        patch.object(ytdlp, "_sync_extract", return_value=info),
        pytest.raises(FetchFailed, match="livestream"),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


async def test_resolve_track_accepts_recorded_was_live(tmp_path: Path) -> None:
    # ``was_live`` / ``post_live`` are downloadable VODs; only active
    # and upcoming streams are rejected.
    info = _track_info(is_live=False, live_status="was_live")
    with patch.object(ytdlp, "_sync_extract", return_value=info):
        track = await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
    assert track.video_id == YTID


async def test_resolve_track_invalid_id_raises_invalid_video_id(tmp_path: Path) -> None:
    info = _track_info(id="../escape")
    with patch.object(ytdlp, "_sync_extract", return_value=info), pytest.raises(InvalidVideoID):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


async def test_resolve_track_missing_id_raises_fetch_failed(tmp_path: Path) -> None:
    info = _track_info()
    info.pop("id")
    with (
        patch.object(ytdlp, "_sync_extract", return_value=info),
        pytest.raises(FetchFailed, match="no video id"),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


async def test_resolve_track_handles_missing_optional_fields(tmp_path: Path) -> None:
    info: dict[str, Any] = {"id": YTID, "webpage_url": TRACK_URL}
    with patch.object(ytdlp, "_sync_extract", return_value=info):
        track = await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
    assert track.title == "Unknown title"
    assert track.uploader == "Unknown uploader"
    assert track.duration_ms == 0


# ---------------------------------------------------------------------------
# Friendly error mapping (DownloadError translation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ERROR: [youtube] X: Sign in to confirm your age", "age-restricted"),
        ("ERROR: Private video. Sign in if you've been granted access.", "private video"),
        (
            "ERROR: [youtube] X: Video unavailable. The uploader has not made it available.",
            "region-blocked or unavailable",
        ),
    ],
)
async def test_resolve_track_maps_known_errors(raw: str, expected: str, tmp_path: Path) -> None:
    with (
        patch.object(ytdlp, "_sync_extract", side_effect=DownloadError(raw)),
        pytest.raises(FetchFailed, match=expected),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


async def test_resolve_track_unknown_download_error_passes_first_line(tmp_path: Path) -> None:
    raw = "ERROR: yt-dlp something went wrong\nsecond line: details\nthird"
    with (
        patch.object(ytdlp, "_sync_extract", side_effect=DownloadError(raw)),
        pytest.raises(FetchFailed) as excinfo,
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
    msg = str(excinfo.value)
    assert "second line" not in msg
    assert "third" not in msg
    assert "ERROR" in msg


async def test_resolve_track_internal_error_logged_and_wrapped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(ytdlp, "_sync_extract", side_effect=RuntimeError("kaboom")),
        caplog.at_level("ERROR"),
        pytest.raises(FetchFailed, match="internal error"),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
    assert any("kaboom" in r.message or "kaboom" in str(r.exc_info) for r in caplog.records)


async def test_resolve_track_extract_returning_none_raises(tmp_path: Path) -> None:
    # ``_sync_extract`` is the layer that converts None → FetchFailed,
    # so patch ``YoutubeDL`` itself to exercise that path.
    fake_ydl = MagicMock()
    fake_ydl.__enter__.return_value = fake_ydl
    fake_ydl.__exit__.return_value = False
    fake_ydl.extract_info.return_value = None
    with (
        patch("ryzic.ytdlp.YoutubeDL", return_value=fake_ydl),
        pytest.raises(FetchFailed, match="no info"),
    ):
        await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)


# ---------------------------------------------------------------------------
# resolve_playlist
# ---------------------------------------------------------------------------


async def test_resolve_playlist_returns_entries(tmp_path: Path) -> None:
    with patch.object(ytdlp, "_sync_extract", return_value=_playlist_info()) as m:
        playlist = await ytdlp.resolve_playlist(PLIST_URL, cache_root=tmp_path)
    assert playlist.playlist_id == "PLabcdefghij"
    assert playlist.title == "Test playlist"
    assert [e.video_id for e in playlist.entries] == ["abcdefghijk", "lmnopqrstuv"]
    # extract_flat=True is required for cheap playlist listing.
    opts, _url = m.call_args.args
    assert opts["extract_flat"] is True
    assert opts["noplaylist"] is False


async def test_resolve_playlist_skips_invalid_entries(tmp_path: Path) -> None:
    info = _playlist_info(
        entries=[
            {"id": "abcdefghijk", "title": "Track A"},
            {"id": "../bad", "title": "Skipped"},  # invalid id
            {"id": None, "title": "Also skipped"},  # missing id
            "not even a dict",  # garbage
            {"id": "lmnopqrstuv", "title": "Track B"},
        ]
    )
    with patch.object(ytdlp, "_sync_extract", return_value=info):
        playlist = await ytdlp.resolve_playlist(PLIST_URL, cache_root=tmp_path)
    assert [e.video_id for e in playlist.entries] == ["abcdefghijk", "lmnopqrstuv"]


async def test_resolve_playlist_missing_id_raises(tmp_path: Path) -> None:
    info = _playlist_info()
    info.pop("id")
    with (
        patch.object(ytdlp, "_sync_extract", return_value=info),
        pytest.raises(FetchFailed, match="no playlist id"),
    ):
        await ytdlp.resolve_playlist(PLIST_URL, cache_root=tmp_path)


async def test_resolve_playlist_empty_entries_returns_empty_list(tmp_path: Path) -> None:
    info = _playlist_info(entries=[])
    with patch.object(ytdlp, "_sync_extract", return_value=info):
        playlist = await ytdlp.resolve_playlist(PLIST_URL, cache_root=tmp_path)
    assert playlist.entries == []


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


async def test_download_rejects_dest_outside_cache_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    outside = tmp_path / "elsewhere" / "file.m4a"
    with (
        patch.object(ytdlp, "_sync_extract") as m,
        pytest.raises(InvalidVideoID, match="escapes cache_root"),
    ):
        await ytdlp.download(TRACK_URL, outside, cache_root=cache_root)
    m.assert_not_called()


async def test_download_rejects_traversal_dest(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    traversal = cache_root / "audio" / ".." / ".." / "etc" / "passwd"
    with patch.object(ytdlp, "_sync_extract") as m, pytest.raises(InvalidVideoID):
        await ytdlp.download(TRACK_URL, traversal, cache_root=cache_root)
    m.assert_not_called()


async def test_download_invokes_extract_with_outtmpl(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    dest = cache_root / "audio" / "dQ" / "dQw4w9WgXcQ.m4a"
    with patch.object(ytdlp, "_sync_extract", return_value=_track_info()) as m:
        await ytdlp.download(TRACK_URL, dest, cache_root=cache_root)
    opts, called_url = m.call_args.args
    assert called_url == TRACK_URL
    assert opts["outtmpl"] == str(dest.resolve())
    assert opts["match_filter"] is ytdlp._reject_livestream_filter
    assert m.call_args.kwargs == {"download": True}


async def test_download_rejects_livestream(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    dest = cache_root / "audio" / "dQ" / "dQw4w9WgXcQ.m4a"
    info = _track_info(is_live=True, live_status="is_live")
    with (
        patch.object(ytdlp, "_sync_extract", return_value=info),
        pytest.raises(FetchFailed, match="livestream"),
    ):
        await ytdlp.download(TRACK_URL, dest, cache_root=cache_root)


# ---------------------------------------------------------------------------
# Frozen YoutubeDL opts (security-critical)
# ---------------------------------------------------------------------------


def test_base_opts_security_critical_settings(tmp_path: Path) -> None:
    opts = ytdlp._base_opts(tmp_path)
    # Cookies MUST stay disabled — security item 13.
    assert opts["cookiefile"] is None
    # Disk-fill caps.
    assert opts["max_filesize"] == 500_000_000
    assert opts["playlist_items"] == "1-1000"
    # No surprise geo unblock.
    assert opts["geo_bypass"] is False
    # Single fragment download — review §4 sqlite/eviction race.
    assert opts["concurrent_fragment_downloads"] == 1
    # Filesystem hardening — restrictfilenames + sandboxed paths.home.
    assert opts["restrictfilenames"] is True
    assert opts["paths"]["home"] == str(tmp_path / "tmp")
    # Codec allowlist for Lavaplayer.
    assert "bestaudio[ext=m4a]" in opts["format"]
    # Defaults that get overridden for playlists.
    assert opts["noplaylist"] is True
    assert opts["extract_flat"] is False


def test_base_opts_does_not_leak_global_state(tmp_path: Path) -> None:
    a = ytdlp._base_opts(tmp_path)
    b = ytdlp._base_opts(tmp_path)
    a["format"] = "mutated"
    assert b["format"] != "mutated"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("first\nsecond", "first"),
        ("   \n  real  \nthird", "real"),
        ("only-line", "only-line"),
        ("", ""),
        ("   \n  ", ""),
    ],
)
def test_first_line_extracts_first_non_empty(raw: str, expected: str) -> None:
    assert ytdlp._first_line(raw) == expected


def test_reject_livestream_filter_returns_reason_for_live() -> None:
    assert ytdlp._reject_livestream_filter({"is_live": True}) == "livestream"
    assert ytdlp._reject_livestream_filter({"live_status": "is_upcoming"}) == "livestream"


def test_reject_livestream_filter_passes_normal_videos() -> None:
    assert ytdlp._reject_livestream_filter({"is_live": False, "live_status": "not_live"}) is None
    assert ytdlp._reject_livestream_filter({}) is None
