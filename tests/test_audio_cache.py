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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from ryzic import audio_cache
from ryzic.audio_cache import AudioCache, sweep_orphans
from ryzic.errors import FetchFailed, InvalidVideoID
from ryzic.ytdlp import TrackInfo

_DEFAULT_PAYLOAD = b"audio-bytes-stand-in" * 4


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


def _patch_download(payload: bytes = _DEFAULT_PAYLOAD) -> Any:
    """Patch :func:`ryzic.audio_cache.download` with a stub writing ``payload``."""

    async def _impl(url: str, dest: Path, *, cache_root: Path) -> None:
        dest.write_bytes(payload)

    return patch.object(audio_cache, "download", side_effect=_impl)


async def _add_track(
    cache: AudioCache,
    track: TrackInfo,
    *,
    payload_bytes: int = 1000,
    advance_seconds: float = 0,
) -> Path:
    """Add ``track`` to ``cache`` with a deterministic payload + optional clock skip.

    Used by eviction tests to create entries with distinct ``last_used_ts``
    values without sleeping.
    """
    with (
        patch("ryzic.audio_cache.time.time", return_value=time.time() + advance_seconds),
        _patch_download(_bytes_payload(payload_bytes)),
    ):
        return await cache.get_or_download(track)


def _bytes_payload(n_bytes: int) -> bytes:
    return b"\x00" * n_bytes


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
    assert row == (
        "Never Gonna Give You Up",
        "Rick Astley",
        213_000,
        len(_DEFAULT_PAYLOAD),
        "audio",
    )


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
        dest.write_bytes(_DEFAULT_PAYLOAD)

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
        raise FetchFailed("ytdlp.error.generic_with_detail", detail="boom")

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


async def test_try_hit_returns_path_and_metadata_and_pins(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        await cache.get_or_download(track)
        await cache.release(track.video_id)
    hit = await cache.try_hit(track.video_id)
    assert hit is not None
    assert hit.path.exists()
    assert hit.track_info.video_id == track.video_id
    assert hit.track_info.title == track.title
    assert hit.track_info.uploader == track.uploader
    assert hit.track_info.duration_ms == track.duration_ms
    # URL synthesized from video_id (the row stores no URL).
    assert hit.track_info.url == f"https://www.youtube.com/watch?v={track.video_id}"
    assert cache._in_use[track.video_id] == 1
    await cache.release(track.video_id)


async def test_try_hit_returns_none_on_miss(cache: AudioCache) -> None:
    assert await cache.try_hit("dQw4w9WgXcQ") is None


async def test_try_hit_returns_none_when_file_missing(cache: AudioCache) -> None:
    track = _track()
    with _patch_download():
        path = await cache.get_or_download(track)
        await cache.release(track.video_id)
    path.unlink()
    assert await cache.try_hit(track.video_id) is None
    # No phantom pin on a stale row.
    assert track.video_id not in cache._in_use


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


# ---------------------------------------------------------------------------
# LRU eviction (acceptance §11.4)
# ---------------------------------------------------------------------------


async def test_eviction_removes_oldest_first(tmp_path: Path) -> None:
    cache = AudioCache(tmp_path, max_bytes=2_500)
    await cache.open()
    try:
        a = _track("aaaaaaaaaaa")
        b = _track("bbbbbbbbbbb")
        c = _track("ccccccccccc")

        path_a = await _add_track(cache, a)
        await cache.release(a.video_id)
        # Slight time gap so a is unambiguously older than b.
        path_b = await _add_track(cache, b, advance_seconds=10)
        await cache.release(b.video_id)
        # Inserting c (1KB) pushes total to 3KB > 2.5KB cap → evict a.
        path_c = await _add_track(cache, c, advance_seconds=20)

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

        path_a = await _add_track(cache, a)
        # NOTE: do not release a — it is "actively playing".
        path_b = await _add_track(cache, b, advance_seconds=10)
        await cache.release(b.video_id)
        # Adding c forces an eviction. a is the LRU oldest but pinned;
        # evictor must skip it and remove b instead.
        path_c = await _add_track(cache, c, advance_seconds=20)

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


async def test_fast_path_pins_before_touch_yields(tmp_path: Path) -> None:
    """Regression for review MEDIUM-1: the fast-path hit must pin BEFORE awaiting
    ``_touch``. Otherwise a concurrent ``_evict_to_fit`` interleaving on the yield
    can delete a file the fast-path just confirmed.

    Strategy: prime an entry, release it (so it is unpinned), then mock ``_touch``
    to block on a barrier. While ``_touch`` is suspended, kick off another
    ``get_or_download`` for a different vid with a tiny cap so the evictor runs.
    The original vid must NOT be evicted because ``_finalize_hit`` pinned it
    sync-before yielding.
    """
    cache = AudioCache(tmp_path, max_bytes=2_500)
    await cache.open()
    try:
        a = _track("aaaaaaaaaaa")
        b = _track("bbbbbbbbbbb")

        # Prime a, then release so it is unpinned and a candidate for eviction.
        path_a = await _add_track(cache, a, payload_bytes=1500)
        await cache.release(a.video_id)
        assert a.video_id not in cache._in_use

        touch_blocked = asyncio.Event()
        release_touch = asyncio.Event()
        original_touch = cache._touch

        async def slow_touch(video_id: str) -> None:
            touch_blocked.set()
            await release_touch.wait()
            await original_touch(video_id)

        # Fast-path hit on a; touch yields; meanwhile a download for b
        # forces eviction. Pinning must already have happened.
        with patch.object(cache, "_touch", side_effect=slow_touch):
            hit_task = asyncio.create_task(cache.get_or_download(a))
            await touch_blocked.wait()
            # _touch is suspended. If pinning happens AFTER _touch, a's
            # _in_use is still 0 and the eviction below would delete it.
            assert cache._in_use[a.video_id] == 1
            # Trigger eviction via b (advance_seconds keeps b's last_used_ts
            # newer; a is the LRU candidate).
            await _add_track(cache, b, payload_bytes=1500, advance_seconds=10)
            release_touch.set()
            result = await hit_task

        assert result == path_a
        assert path_a.exists()
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
        await _add_track(cache, a, payload_bytes=1000)
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
        ):
            await _add_track(cache, b, payload_bytes=1000, advance_seconds=10)

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
        audio_cache._audio_path(cache_root, "dQw4w9WgXcQ", "audio")


def test_audio_path_validates_video_id_first(tmp_path: Path) -> None:
    with pytest.raises(InvalidVideoID):
        audio_cache._audio_path(tmp_path, "../../etc/passwd", "audio")


def test_audio_path_layout(tmp_path: Path) -> None:
    p = audio_cache._audio_path(tmp_path, "dQw4w9WgXcQ", "audio")
    assert p == tmp_path / "audio" / "dQ" / "dQw4w9WgXcQ.audio"


async def test_open_rejects_symlink_cache_root(tmp_path: Path) -> None:
    # Defense-in-depth: a symlink AT cache_root would land all writes
    # at the symlink target (security review LOW-6).
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    cache = AudioCache(link, max_bytes=10_000)
    with pytest.raises(RuntimeError, match="symlink"):
        await cache.open()


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
    old_orphan = tmp_path / "audio" / "zz" / "zzzzzzzzzzz.audio"
    old_orphan.parent.mkdir(parents=True, exist_ok=True)
    old_orphan.write_bytes(_DEFAULT_PAYLOAD)
    old_mtime = time.time() - audio_cache._ORPHAN_MIN_AGE_S - 100
    os.utime(old_orphan, (old_mtime, old_mtime))

    # Untracked young file → preserved (avoids racing concurrent download).
    young_orphan = tmp_path / "audio" / "yy" / "yyyyyyyyyyy.audio"
    young_orphan.parent.mkdir(parents=True, exist_ok=True)
    young_orphan.write_bytes(_DEFAULT_PAYLOAD)

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
    young = audio_dir / "abcdefghijk.audio"
    young.write_bytes(_DEFAULT_PAYLOAD)
    old = audio_dir / "lmnopqrstuv.audio"
    old.write_bytes(_DEFAULT_PAYLOAD)
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


async def test_sweep_orphans_reaps_old_tmp_files(tmp_path: Path) -> None:
    # Crashed downloads leave .partial files in tmp/; sweep must reap
    # them once they age past the download window (security LOW-7).
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    old_partial = tmp_dir / "aaaaaaaaaaa.partial"
    old_partial.write_bytes(b"partial-bytes")
    old_mtime = time.time() - audio_cache._ORPHAN_MIN_AGE_S - 100
    os.utime(old_partial, (old_mtime, old_mtime))

    young_partial = tmp_dir / "bbbbbbbbbbb.partial"
    young_partial.write_bytes(b"partial-bytes")

    assert await sweep_orphans(tmp_path) == 1
    assert not old_partial.exists()
    assert young_partial.exists()


async def test_download_refreshes_mtime_after_replace(tmp_path: Path) -> None:
    # Security LOW-3: ``os.replace`` preserves the source mtime, so a
    # slow download whose tmp file is already >1h old would land in
    # audio/ with that old mtime — a sweep racing the INSERT could
    # reap it. The fix refreshes mtime via ``os.utime``.
    cache = AudioCache(tmp_path, max_bytes=10_000)
    await cache.open()
    try:
        track = _track()
        old_mtime = time.time() - audio_cache._ORPHAN_MIN_AGE_S - 100

        async def slow_download(url: str, dest: Path, *, cache_root: Path) -> None:
            dest.write_bytes(_DEFAULT_PAYLOAD)
            os.utime(dest, (old_mtime, old_mtime))

        with patch.object(audio_cache, "download", side_effect=slow_download):
            path = await cache.get_or_download(track)

        assert path.stat().st_mtime > old_mtime + 100
    finally:
        await cache.close()


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------


async def test_singleton_round_trip(tmp_path: Path) -> None:
    # Set/get/clear contract: PR6a's /play depends on
    # ``get_audio_cache()`` returning the bot.py-installed instance.
    assert audio_cache.get_audio_cache() is None
    cache = AudioCache(tmp_path, max_bytes=10_000)
    await cache.open()
    try:
        audio_cache.set_audio_cache(cache)
        assert audio_cache.get_audio_cache() is cache
        audio_cache.set_audio_cache(None)
        assert audio_cache.get_audio_cache() is None
    finally:
        await cache.close()
