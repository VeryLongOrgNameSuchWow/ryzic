"""Playlist metadata cache tests (M1 §5).

Live yt-dlp is mocked — these tests do not hit the network. We exercise
round-tripping, playlist_id validation (regex + path safety), the 24h
staleness boundary, and the live-first fallback flow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ryzic import playlist_cache
from ryzic.errors import FetchFailed, InvalidVideoID
from ryzic.ytdlp import PlaylistInfo, TrackInfo

PLIST_ID = "PLabcdefghij"
PLIST_URL = f"https://www.youtube.com/playlist?list={PLIST_ID}"


def _track(video_id: str = "dQw4w9WgXcQ", **overrides: object) -> TrackInfo:
    base: dict[str, object] = {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration_ms": 213_000,
    }
    base.update(overrides)
    return TrackInfo(**base)  # type: ignore[arg-type]


def _playlist(playlist_id: str = PLIST_ID, n_tracks: int = 2) -> PlaylistInfo:
    base_ids = ["dQw4w9WgXcQ", "abcdefghijk", "lmnopqrstuv"]
    entries = [_track(video_id=base_ids[i]) for i in range(n_tracks)]
    return PlaylistInfo(playlist_id=playlist_id, title="Test playlist", entries=entries)


# ---------------------------------------------------------------------------
# Round-trip: write then read returns equal payload
# ---------------------------------------------------------------------------


async def test_round_trip_preserves_playlist(tmp_path: Path) -> None:
    info = _playlist()
    await playlist_cache.write(PLIST_ID, info, tmp_path)
    loaded = await playlist_cache.read(PLIST_ID, tmp_path)
    assert loaded == info


async def test_round_trip_preserves_unicode(tmp_path: Path) -> None:
    info = PlaylistInfo(
        playlist_id=PLIST_ID,
        title="プレイリスト ♪",
        entries=[_track(title="曲名 — café")],
    )
    await playlist_cache.write(PLIST_ID, info, tmp_path)
    loaded = await playlist_cache.read(PLIST_ID, tmp_path)
    assert loaded == info


async def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert await playlist_cache.read(PLIST_ID, tmp_path) is None


async def test_read_malformed_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "playlists" / f"{PLIST_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert await playlist_cache.read(PLIST_ID, tmp_path) is None


async def test_read_structurally_invalid_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "playlists" / f"{PLIST_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"playlist_id": PLIST_ID}), encoding="utf-8")
    assert await playlist_cache.read(PLIST_ID, tmp_path) is None


async def test_write_creates_playlists_directory(tmp_path: Path) -> None:
    assert not (tmp_path / "playlists").exists()
    await playlist_cache.write(PLIST_ID, _playlist(), tmp_path)
    assert (tmp_path / "playlists" / f"{PLIST_ID}.json").is_file()


async def test_write_atomic_replaces_existing_entry(tmp_path: Path) -> None:
    await playlist_cache.write(PLIST_ID, _playlist(n_tracks=1), tmp_path)
    await playlist_cache.write(PLIST_ID, _playlist(n_tracks=3), tmp_path)
    loaded = await playlist_cache.read(PLIST_ID, tmp_path)
    assert loaded is not None
    assert len(loaded.entries) == 3


async def test_write_rejects_mismatched_playlist_id(tmp_path: Path) -> None:
    info = _playlist(playlist_id="PLotherplaylist")
    with pytest.raises(InvalidVideoID, match="mismatch"):
        await playlist_cache.write(PLIST_ID, info, tmp_path)


async def test_write_persists_fetched_at_as_int(tmp_path: Path) -> None:
    before = int(time.time())
    await playlist_cache.write(PLIST_ID, _playlist(), tmp_path)
    after = int(time.time())
    payload = json.loads((tmp_path / "playlists" / f"{PLIST_ID}.json").read_text(encoding="utf-8"))
    assert isinstance(payload["fetched_at"], int)
    assert before <= payload["fetched_at"] <= after


# ---------------------------------------------------------------------------
# playlist_id validation: regex + path-traversal rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "playlist_id",
    [
        "PLabcdefghij",  # 12-char canonical
        "a" * 10,  # min length
        "Z" * 50,  # max length
        "PL_-abc-_DEF1234567",
    ],
)
async def test_validate_accepts_valid(playlist_id: str, tmp_path: Path) -> None:
    info = PlaylistInfo(playlist_id=playlist_id, title="t", entries=[])
    await playlist_cache.write(playlist_id, info, tmp_path)
    assert await playlist_cache.read(playlist_id, tmp_path) == info


@pytest.mark.parametrize(
    "playlist_id",
    [
        "",
        "short",  # too short (<10)
        "a" * 51,  # too long (>50)
        "../../etc/passwd",  # path traversal
        "abc/../bad",
        "abc def",  # space
        "abc.json",  # dot — would compose a different file name
        "abc%20def",  # url-encoded
        "abc?def",  # query fragment
        "abc/def",  # slash
        "abc\x00def",  # nul byte
        # Default-mode ``$`` matches before a final ``\n``; ``fullmatch``
        # is the fix. These cases pin the regression.
        "PLabcdefghi\n",
        "PLabcdefghi\r\n",
        "\nPLabcdefghi",
    ],
)
def test_validate_playlist_id_rejects_invalid(playlist_id: str) -> None:
    with pytest.raises(InvalidVideoID):
        playlist_cache._validate_playlist_id(playlist_id)


async def test_traversal_id_does_not_create_file_outside_cache(tmp_path: Path) -> None:
    # Defense-in-depth: even if validation regressed, the file should
    # never land outside ``cache_root``.
    bad_id = "../escape"
    info = PlaylistInfo(playlist_id=bad_id, title="t", entries=[])
    with pytest.raises(InvalidVideoID):
        await playlist_cache.write(bad_id, info, tmp_path)
    # Nothing leaked into the parent.
    assert not (tmp_path.parent / "escape.json").exists()


async def test_fetch_with_fallback_rejects_trailing_newline_list_param(
    tmp_path: Path,
) -> None:
    # ``%0a`` decodes to ``\n``; a buggy ``$`` anchor would let it slip
    # into ``_path_for`` and pollute the cache namespace + the log line.
    bad_url = f"https://www.youtube.com/playlist?list={PLIST_ID}%0a"
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=FetchFailed("x")),
        pytest.raises(FetchFailed),
    ):
        await playlist_cache.fetch_with_fallback(bad_url, cache_root=tmp_path)


async def test_extract_playlist_id_picks_first_when_list_param_repeats() -> None:
    # ``parse_qs`` returns all values; we deterministically pick the
    # first. If that first is invalid we fail-closed (no scanning).
    first_bad = f"https://www.youtube.com/playlist?list=../bad&list={PLIST_ID}"
    assert playlist_cache._extract_playlist_id(first_bad) is None
    first_good = f"https://www.youtube.com/playlist?list={PLIST_ID}&list=PLother_______"
    assert playlist_cache._extract_playlist_id(first_good) == PLIST_ID


def _write_with_fetched_at(tmp_path: Path, fetched_at: int) -> PlaylistInfo:
    info = _playlist()
    payload = {
        "playlist_id": info.playlist_id,
        "title": info.title,
        "fetched_at": fetched_at,
        "entries": [
            {
                "video_id": e.video_id,
                "url": e.url,
                "title": e.title,
                "uploader": e.uploader,
                "duration_ms": e.duration_ms,
            }
            for e in info.entries
        ],
    }
    path = tmp_path / "playlists" / f"{info.playlist_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return info


def test_is_stale_just_under_24h_is_fresh(tmp_path: Path) -> None:
    now = 1_700_000_000
    fetched_at = now - (24 * 60 * 60 - 1)  # 23h59m59s old
    info = _write_with_fetched_at(tmp_path, fetched_at)
    with patch.object(playlist_cache.time, "time", return_value=now):
        assert playlist_cache.is_stale(info, cache_root=tmp_path) is False


def test_is_stale_exactly_24h_is_fresh(tmp_path: Path) -> None:
    # Boundary is strict ``>``: exactly 24h old is still fresh.
    now = 1_700_000_000
    fetched_at = now - 24 * 60 * 60
    info = _write_with_fetched_at(tmp_path, fetched_at)
    with patch.object(playlist_cache.time, "time", return_value=now):
        assert playlist_cache.is_stale(info, cache_root=tmp_path) is False


def test_is_stale_just_over_24h_is_stale(tmp_path: Path) -> None:
    now = 1_700_000_000
    fetched_at = now - (24 * 60 * 60 + 1)  # 24h00m01s old
    info = _write_with_fetched_at(tmp_path, fetched_at)
    with patch.object(playlist_cache.time, "time", return_value=now):
        assert playlist_cache.is_stale(info, cache_root=tmp_path) is True


def test_is_stale_missing_file_is_stale(tmp_path: Path) -> None:
    info = _playlist()
    assert playlist_cache.is_stale(info, cache_root=tmp_path) is True


def test_is_stale_missing_fetched_at_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "playlists" / f"{PLIST_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"playlist_id": PLIST_ID, "title": "t", "entries": []}),
        encoding="utf-8",
    )
    assert playlist_cache.is_stale(_playlist(), cache_root=tmp_path) is True


# ---------------------------------------------------------------------------
# fetch_with_fallback: live-first, cache-on-failure
# ---------------------------------------------------------------------------


async def test_fetch_with_fallback_live_success_writes_cache(tmp_path: Path) -> None:
    info = _playlist()
    with patch.object(playlist_cache, "resolve_playlist", return_value=info) as m:
        result, used_cache = await playlist_cache.fetch_with_fallback(
            PLIST_URL, cache_root=tmp_path
        )
    assert result == info
    assert used_cache is False
    m.assert_awaited_once_with(PLIST_URL, cache_root=tmp_path)
    # Cache populated as a side effect.
    cached = await playlist_cache.read(PLIST_ID, tmp_path)
    assert cached == info


async def test_fetch_with_fallback_uses_cache_on_yt_dlp_failure(tmp_path: Path) -> None:
    cached_info = _playlist(n_tracks=3)
    await playlist_cache.write(PLIST_ID, cached_info, tmp_path)

    with patch.object(
        playlist_cache,
        "resolve_playlist",
        side_effect=FetchFailed("yt-dlp exploded"),
    ) as m:
        result, used_cache = await playlist_cache.fetch_with_fallback(
            PLIST_URL, cache_root=tmp_path
        )
    assert result == cached_info
    assert used_cache is True
    m.assert_awaited_once()


async def test_fetch_with_fallback_returns_stale_cache_unconditionally(
    tmp_path: Path,
) -> None:
    # Per spec: fallback ignores TTL — returns ANY cache hit, even
    # ancient ones; the bool flag + ``is_stale`` drive the embed
    # warning, not the fallback decision.
    cached = _playlist(n_tracks=2)
    await playlist_cache.write(PLIST_ID, cached, tmp_path)
    # Backdate the on-disk fetched_at to a week ago.
    path = tmp_path / "playlists" / f"{PLIST_ID}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fetched_at"] = int(time.time()) - 7 * 24 * 60 * 60
    path.write_text(json.dumps(payload), encoding="utf-8")

    with patch.object(playlist_cache, "resolve_playlist", side_effect=FetchFailed("down")):
        result, used_cache = await playlist_cache.fetch_with_fallback(
            PLIST_URL, cache_root=tmp_path
        )
    assert result == cached
    assert used_cache is True
    assert playlist_cache.is_stale(result, cache_root=tmp_path) is True


async def test_fetch_with_fallback_reraises_when_no_cache(tmp_path: Path) -> None:
    original = FetchFailed("That playlist is empty or private.")
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=original),
        pytest.raises(FetchFailed) as excinfo,
    ):
        await playlist_cache.fetch_with_fallback(PLIST_URL, cache_root=tmp_path)
    assert excinfo.value is original


async def test_fetch_with_fallback_reraises_when_url_has_no_list_param(
    tmp_path: Path,
) -> None:
    # Without ``list=``, we can't derive a playlist_id to look up — so
    # there's no fallback path, even if some unrelated cache file exists.
    await playlist_cache.write(PLIST_ID, _playlist(), tmp_path)
    no_list = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=FetchFailed("x")),
        pytest.raises(FetchFailed),
    ):
        await playlist_cache.fetch_with_fallback(no_list, cache_root=tmp_path)


async def test_fetch_with_fallback_reraises_when_list_param_invalid(
    tmp_path: Path,
) -> None:
    # Adversarial ``list=`` values that fail the regex must NOT be used
    # to compose a path — even for a read.
    bad_url = "https://www.youtube.com/playlist?list=../../etc"
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=FetchFailed("x")),
        pytest.raises(FetchFailed),
    ):
        await playlist_cache.fetch_with_fallback(bad_url, cache_root=tmp_path)


async def test_fetch_with_fallback_propagates_unexpected_exceptions(
    tmp_path: Path,
) -> None:
    # Non-FetchFailed exceptions (e.g. bugs in the wrapper) must NOT
    # silently swallow — only the spec-defined yt-dlp failure path
    # triggers fallback.
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=RuntimeError("bug")),
        pytest.raises(RuntimeError, match="bug"),
    ):
        await playlist_cache.fetch_with_fallback(PLIST_URL, cache_root=tmp_path)
