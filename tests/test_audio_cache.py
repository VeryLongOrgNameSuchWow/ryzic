"""Audio cache tests (M1 §4).

yt-dlp's :func:`ryzic.ytdlp.download` is patched throughout — these
tests never hit the network. We exercise: hit/miss paths, concurrent
download deduplication, LRU eviction with in-use pinning, the orphan
sweep age guard, the path-safety relative-to check, sqlite WAL setup,
and connection lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from ryzic import audio_cache
from ryzic.audio_cache import AudioCache, sweep_orphans
from ryzic.errors import FetchFailed, InvalidVideoID
from ryzic.ytdlp import TrackInfo

# Magic bytes the cache's ``_detect_ext`` recognizes — used by the fake
# downloader so the test cache produces realistic per-codec layouts.
_M4A_HEAD = b"\x00\x00\x00\x18ftypM4A "
_OPUS_HEAD = b"OggSOpusHead-padding"
_WEBM_HEAD = b"\x1a\x45\xdf\xa3\x9fB\x86\x81"


def _track(video_id: str = "dQw4w9WgXcQ", **overrides: Any) -> TrackInfo:
    base: dict[str, Any] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration_ms": 213_000,
    }
    base.update(overrides)
    return TrackInfo(**base)


def _fake_download(payload: bytes = _M4A_HEAD * 8) -> Callable[..., Any]:
    """Return an async stub mimicking :func:`ryzic.ytdlp.download`.

    Writes ``payload`` to ``dest`` so ``_detect_ext`` and the size
    measurement run against real bytes.
    """

    async def _impl(url: str, dest: Path, *, cache_root: Path) -> None:
        dest.write_bytes(payload)

    return _impl


def _patch_download(payload: bytes = _M4A_HEAD * 8) -> Any:
    """Patch :func:`ryzic.audio_cache.download` with a fake writing ``payload``."""
    return patch.object(audio_cache, "download", side_effect=_fake_download(payload))


async def _read_one(
    db_path: Path, sql: str, params: tuple[Any, ...] = ()
) -> tuple[Any, ...] | None:
    async with aiosqlite.connect(db_path) as conn, conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return tuple(row) if row is not None else None


async def _read_many(
    db_path: Path, sql: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    async with aiosqlite.connect(db_path) as conn, conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [tuple(r) for r in rows]


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[AudioCache]:
    """A cache with a generous 100MB cap — tests that need eviction set their own."""
    c = AudioCache(tmp_path, max_bytes=100_000_000)
    await c.open()
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# open / close lifecycle
# ---------------------------------------------------------------------------


async def test_open_creates_directories_and_schema(tmp_path: Path) -> None:
    c = AudioCache(tmp_path, max_bytes=10_000)
    await c.open()
    try:
        assert (tmp_path / "audio").is_dir()
        assert (tmp_path / "tmp").is_dir()
        assert (tmp_path / "index.sqlite").is_file()
        rows = await _read_many(
            tmp_path / "index.sqlite",
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
        assert "entries" in {r[0] for r in rows}
    finally:
        await c.close()


async def test_open_sets_wal_pragmas(tmp_path: Path) -> None:
    c = AudioCache(tmp_path, max_bytes=10_000)
    await c.open()
    try:
        row = await _read_one(tmp_path / "index.sqlite", "PRAGMA journal_mode")
        assert row is not None
        assert row[0].lower() == "wal"
    finally:
        await c.close()


async def test_open_is_idempotent_across_restarts(tmp_path: Path) -> None:
    # Simulate restart: open, close, re-open.
    c = AudioCache(tmp_path, max_bytes=10_000)
    await c.open()
    await c.close()
    c = AudioCache(tmp_path, max_bytes=10_000)
    await c.open()
    await c.close()


async def test_methods_raise_when_not_opened(tmp_path: Path) -> None:
    c = AudioCache(tmp_path, max_bytes=10_000)
    with pytest.raises(RuntimeError, match="open"):
        await c.get_or_download(_track())


# ---------------------------------------------------------------------------
# get_or_download — happy path + miss / hit
# ---------------------------------------------------------------------------


async def test_get_or_download_miss_writes_file_and_row(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        path = await cache.get_or_download(track)
    assert path.exists()
    assert path.is_relative_to(cache.cache_root / "audio")
    # Two-char shard directory.
    assert path.parent.name == track.video_id[:2]
    assert path.name.startswith(track.video_id)
    row = await _read_one(
        cache.cache_root / "index.sqlite",
        "SELECT title, uploader, duration_ms, bytes, ext FROM entries WHERE video_id = ?",
        (track.video_id,),
    )
    assert row == ("Never Gonna Give You Up", "Rick Astley", 213_000, len(_M4A_HEAD * 8), "m4a")


async def test_get_or_download_hit_skips_download(cache: AudioCache) -> None:
    track = _track()
    with _patch_download() as m:
        await cache.get_or_download(track)
        await cache.release(track.video_id)
        await cache.get_or_download(track)
    assert m.call_count == 1


async def test_hit_touches_last_used_ts(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        await cache.get_or_download(track)
        await cache.release(track.video_id)
    row = await _read_one(
        cache.cache_root / "index.sqlite",
        "SELECT last_used_ts FROM entries WHERE video_id = ?",
        (track.video_id,),
    )
    assert row is not None
    first_ts = row[0]
    with (
        patch("ryzic.audio_cache.time.time", return_value=first_ts + 100),
        _patch_download(),
    ):
        await cache.get_or_download(track)
    row = await _read_one(
        cache.cache_root / "index.sqlite",
        "SELECT last_used_ts FROM entries WHERE video_id = ?",
        (track.video_id,),
    )
    assert row is not None
    assert row[0] == first_ts + 100


async def test_hit_increments_in_use(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        await cache.get_or_download(track)
        await cache.get_or_download(track)
    assert cache._in_use[track.video_id] == 2


async def test_stale_row_with_missing_file_is_repaired(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        path = await cache.get_or_download(track)
        await cache.release(track.video_id)
    # Manually delete the file behind the cache's back.
    path.unlink()
    with _patch_download() as m:
        new_path = await cache.get_or_download(track)
    # Re-downloads, repairs the row (still keyed on the same id).
    assert m.call_count == 1
    assert new_path.exists()


# ---------------------------------------------------------------------------
# Concurrent download deduplication (acceptance §11.3)
# ---------------------------------------------------------------------------


async def test_concurrent_get_or_download_triggers_one_download(cache: AudioCache) -> None:
    track = _track()
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    call_count = 0

    async def slow_download(url: str, dest: Path, *, cache_root: Path) -> None:
        nonlocal call_count
        call_count += 1
        download_started.set()
        await release_download.wait()
        dest.write_bytes(_M4A_HEAD * 8)

    with patch.object(audio_cache, "download", side_effect=slow_download):
        # Three concurrent /play calls for the same URL.
        task_a = asyncio.create_task(cache.get_or_download(track))
        task_b = asyncio.create_task(cache.get_or_download(track))
        task_c = asyncio.create_task(cache.get_or_download(track))
        await download_started.wait()
        # All three are now blocked on the lock or the running download.
        release_download.set()
        results = await asyncio.gather(task_a, task_b, task_c)

    assert call_count == 1
    assert results[0] == results[1] == results[2]
    assert cache._in_use[track.video_id] == 3


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_download_failure_cleans_tmp_and_raises(cache: AudioCache) -> None:
    track = _track()

    async def failing_download(url: str, dest: Path, *, cache_root: Path) -> None:
        # Simulate yt-dlp writing partial bytes before exploding.
        dest.write_bytes(b"partial")
        raise FetchFailed("boom")

    with (
        patch.object(audio_cache, "download", side_effect=failing_download),
        pytest.raises(FetchFailed, match="boom"),
    ):
        await cache.get_or_download(track)
    assert list((cache.cache_root / "tmp").iterdir()) == []
    row = await _read_one(
        cache.cache_root / "index.sqlite",
        "SELECT COUNT(*) FROM entries WHERE video_id = ?",
        (track.video_id,),
    )
    assert row is not None
    assert row[0] == 0


async def test_download_succeeds_but_file_missing_raises(cache: AudioCache) -> None:
    track = _track()

    async def lying_download(url: str, dest: Path, *, cache_root: Path) -> None:
        return None

    with (
        patch.object(audio_cache, "download", side_effect=lying_download),
        pytest.raises(FetchFailed, match="missing"),
    ):
        await cache.get_or_download(track)


async def test_invalid_video_id_rejected_before_io(cache: AudioCache) -> None:
    track = _track(video_id="../../etc/passwd")
    with patch.object(audio_cache, "download") as m, pytest.raises(InvalidVideoID):
        await cache.get_or_download(track)
    m.assert_not_called()


# ---------------------------------------------------------------------------
# release / release_many
# ---------------------------------------------------------------------------


async def test_release_decrements_and_pops_at_zero(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        await cache.get_or_download(track)
        await cache.get_or_download(track)
    assert cache._in_use[track.video_id] == 2
    await cache.release(track.video_id)
    assert cache._in_use[track.video_id] == 1
    await cache.release(track.video_id)
    # Counter key removed entirely so the dict can't grow unbounded.
    assert track.video_id not in cache._in_use


async def test_release_idempotent_after_zero(cache: AudioCache) -> None:
    # Releasing more times than acquires is a caller bug but must not raise.
    await cache.release("dQw4w9WgXcQ")
    await cache.release("dQw4w9WgXcQ")


async def test_release_many_decrements_each(cache: AudioCache) -> None:
    a = _track("aaaaaaaaaaa")
    b = _track("bbbbbbbbbbb")
    c = _track("ccccccccccc")
    with _patch_download():
        await cache.get_or_download(a)
        await cache.get_or_download(b)
        await cache.get_or_download(b)
        await cache.get_or_download(c)
    # a:1, b:2, c:1
    await cache.release_many([a.video_id, b.video_id, c.video_id])
    # a:0(removed), b:1, c:0(removed)
    assert a.video_id not in cache._in_use
    assert cache._in_use[b.video_id] == 1
    assert c.video_id not in cache._in_use


# ---------------------------------------------------------------------------
# LRU eviction (acceptance §11.4)
# ---------------------------------------------------------------------------


def _bytes_payload(n_bytes: int) -> bytes:
    # Real m4a-ish header so _detect_ext picks "m4a"; padded to size.
    head = _M4A_HEAD
    return head + (b"\x00" * max(0, n_bytes - len(head)))


async def test_eviction_removes_oldest_first(tmp_path: Path) -> None:
    cache = AudioCache(tmp_path, max_bytes=2_500)
    await cache.open()
    try:
        a = _track("aaaaaaaaaaa")
        b = _track("bbbbbbbbbbb")
        c = _track("ccccccccccc")

        with _patch_download(_bytes_payload(1000)):
            path_a = await cache.get_or_download(a)
            await cache.release(a.video_id)
        # Slight time gap so a is unambiguously older than b.
        with (
            patch("ryzic.audio_cache.time.time", return_value=time.time() + 10),
            _patch_download(_bytes_payload(1000)),
        ):
            path_b = await cache.get_or_download(b)
            await cache.release(b.video_id)
        # Inserting c (1KB) pushes total to 3KB > 2.5KB cap → evict a.
        with (
            patch("ryzic.audio_cache.time.time", return_value=time.time() + 20),
            _patch_download(_bytes_payload(1000)),
        ):
            path_c = await cache.get_or_download(c)

        assert not path_a.exists()
        assert path_b.exists()
        assert path_c.exists()

        rows = await _read_many(cache.cache_root / "index.sqlite", "SELECT video_id FROM entries")
        ids = {r[0] for r in rows}
        assert a.video_id not in ids
        assert {b.video_id, c.video_id} <= ids
    finally:
        await cache.close()


async def test_eviction_skips_in_use_entries(tmp_path: Path) -> None:
    cache = AudioCache(tmp_path, max_bytes=2_500)
    await cache.open()
    try:
        a = _track("aaaaaaaaaaa")
        b = _track("bbbbbbbbbbb")
        c = _track("ccccccccccc")
        with _patch_download(_bytes_payload(1000)):
            path_a = await cache.get_or_download(a)
        # NOTE: do not release a — it is "actively playing".
        with (
            patch("ryzic.audio_cache.time.time", return_value=time.time() + 10),
            _patch_download(_bytes_payload(1000)),
        ):
            path_b = await cache.get_or_download(b)
            await cache.release(b.video_id)
        # Adding c forces an eviction. a is the LRU oldest but pinned;
        # evictor must skip it and remove b instead.
        with (
            patch("ryzic.audio_cache.time.time", return_value=time.time() + 20),
            _patch_download(_bytes_payload(1000)),
        ):
            path_c = await cache.get_or_download(c)

        assert path_a.exists()  # pinned, survived
        assert not path_b.exists()  # evicted in a's place
        assert path_c.exists()
    finally:
        await cache.close()


async def test_just_inserted_file_not_evicted_due_to_pin(tmp_path: Path) -> None:
    """Regression for review §4: the just-finished download must be pinned BEFORE
    the evictor runs, or its own row (newest, but refcount 0 if pinning happened
    later) could be the only candidate when the cap is already exceeded."""
    # Cap small enough that a SINGLE entry blows it. The evictor must
    # still leave the just-inserted file alone because it is pinned.
    cache = AudioCache(tmp_path, max_bytes=100)
    await cache.open()
    try:
        track = _track()
        with _patch_download(_bytes_payload(1000)):
            path = await cache.get_or_download(track)
        assert path.exists()
        row = await _read_one(cache.cache_root / "index.sqlite", "SELECT video_id FROM entries")
        assert row is not None
        assert row[0] == track.video_id
    finally:
        await cache.close()


async def test_eviction_unlink_failure_does_not_drop_row(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = AudioCache(tmp_path, max_bytes=500)
    await cache.open()
    try:
        a = _track("aaaaaaaaaaa")
        b = _track("bbbbbbbbbbb")
        with _patch_download(_bytes_payload(1000)):
            await cache.get_or_download(a)
            await cache.release(a.video_id)
        # Patch unlink to fail when the evictor tries to remove a's
        # file. The row must stay so we can retry — silent drop would
        # corrupt the index.
        original_unlink = Path.unlink

        def flaky_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
            if self.name.startswith(a.video_id):
                raise OSError("disk on fire")
            original_unlink(self, *args, **kwargs)

        with (
            caplog.at_level("WARNING"),
            patch.object(Path, "unlink", flaky_unlink),
            _patch_download(_bytes_payload(1000)),
        ):
            await cache.get_or_download(b)

        row = await _read_one(
            cache.cache_root / "index.sqlite",
            "SELECT video_id FROM entries WHERE video_id = ?",
            (a.video_id,),
        )
        assert row is not None
        assert any("eviction failed" in r.message for r in caplog.records)
    finally:
        await cache.close()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_audio_path_rejects_traversal_via_symlink(tmp_path: Path) -> None:
    # A symlink at audio/ID/ pointing outside cache_root would let a
    # validated video_id still resolve outside the sandbox. The
    # post-construction relative_to() check catches this.
    cache_root = tmp_path / "cache"
    elsewhere = tmp_path / "elsewhere"
    cache_root.mkdir()
    elsewhere.mkdir()
    (cache_root / "audio").mkdir()
    (cache_root / "audio" / "dQ").symlink_to(elsewhere)
    with pytest.raises(InvalidVideoID, match="escapes cache_root"):
        audio_cache._audio_path(cache_root, "dQw4w9WgXcQ", "m4a")


def test_audio_path_validates_video_id_first(tmp_path: Path) -> None:
    with pytest.raises(InvalidVideoID):
        audio_cache._audio_path(tmp_path, "../../etc/passwd", "m4a")


def test_audio_path_layout(tmp_path: Path) -> None:
    p = audio_cache._audio_path(tmp_path, "dQw4w9WgXcQ", "m4a")
    assert p == tmp_path / "audio" / "dQ" / "dQw4w9WgXcQ.m4a"


# ---------------------------------------------------------------------------
# _detect_ext
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_M4A_HEAD * 2, "m4a"),
        (_OPUS_HEAD, "opus"),
        (_WEBM_HEAD * 2, "webm"),
        (b"ID3" + b"\x00" * 20, "mp3"),
        (b"\xff\xf1" + b"\x00" * 20, "mp3"),  # AAC ADTS / MPEG sync also matches.
        (b"junk garbage nothing", "audio"),
        (b"", "audio"),
    ],
)
def test_detect_ext_recognizes_known_codecs(tmp_path: Path, payload: bytes, expected: str) -> None:
    p = tmp_path / "sample"
    p.write_bytes(payload)
    assert audio_cache._detect_ext(p) == expected


def test_detect_ext_handles_unreadable_file(tmp_path: Path) -> None:
    # Path that does not exist → OSError → fallback ext.
    assert audio_cache._detect_ext(tmp_path / "does-not-exist") == "audio"


# ---------------------------------------------------------------------------
# sweep_orphans
# ---------------------------------------------------------------------------


async def test_sweep_orphans_handles_missing_audio_dir(tmp_path: Path) -> None:
    assert await sweep_orphans(tmp_path) == 0


async def test_sweep_orphans_deletes_old_unreferenced_files(tmp_path: Path) -> None:
    cache = AudioCache(tmp_path, max_bytes=10_000_000)
    await cache.open()
    try:
        track = _track()
        with _patch_download():
            tracked_path = await cache.get_or_download(track)
            await cache.release(track.video_id)
    finally:
        await cache.close()

    # Untracked old file → deleted.
    old_orphan = tmp_path / "audio" / "zz" / "zzzzzzzzzzz.m4a"
    old_orphan.parent.mkdir(parents=True, exist_ok=True)
    old_orphan.write_bytes(_M4A_HEAD)
    old_mtime = time.time() - audio_cache._ORPHAN_MIN_AGE_S - 100
    os.utime(old_orphan, (old_mtime, old_mtime))

    # Untracked young file → preserved (avoids racing concurrent download).
    young_orphan = tmp_path / "audio" / "yy" / "yyyyyyyyyyy.m4a"
    young_orphan.parent.mkdir(parents=True, exist_ok=True)
    young_orphan.write_bytes(_M4A_HEAD)

    deleted = await sweep_orphans(tmp_path)
    assert deleted == 1
    assert not old_orphan.exists()
    assert young_orphan.exists()
    assert tracked_path.exists()


async def test_sweep_orphans_works_with_no_db(tmp_path: Path) -> None:
    # No index.sqlite → every audio file is "untracked", but young
    # files still survive on age guard.
    audio_dir = tmp_path / "audio" / "ab"
    audio_dir.mkdir(parents=True)
    young = audio_dir / "abcdefghijk.m4a"
    young.write_bytes(_M4A_HEAD)
    old = audio_dir / "lmnopqrstuv.m4a"
    old.write_bytes(_M4A_HEAD)
    old_mtime = time.time() - audio_cache._ORPHAN_MIN_AGE_S - 100
    os.utime(old, (old_mtime, old_mtime))

    assert await sweep_orphans(tmp_path) == 1
    assert young.exists()
    assert not old.exists()


async def test_sweep_orphans_skips_directories(tmp_path: Path) -> None:
    # Empty subdirectories under audio/ must not be unlinked or counted.
    (tmp_path / "audio" / "ab").mkdir(parents=True)
    assert await sweep_orphans(tmp_path) == 0
    assert (tmp_path / "audio" / "ab").is_dir()
