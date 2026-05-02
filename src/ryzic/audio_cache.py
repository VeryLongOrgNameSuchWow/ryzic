"""SQLite-backed LRU audio cache (per M1 §4).

The cache holds the only audio bytes the bot needs to play, keyed by
yt-dlp's ``video_id``. Concurrent ``/play`` invocations for the same
URL collapse to a single download via per-video :class:`asyncio.Lock`,
and an in-memory :class:`collections.Counter` keeps the active queue's
files pinned so eviction never deletes a file Lavalink is currently
streaming.

State (locks, in-use refcounts) is in-memory only; SQLite is the
durable index. The cache deliberately survives yt-dlp breakage — there
is no TTL, only an LRU + size cap (per ``M1-simplify.md`` §4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Final

import aiosqlite

from .errors import FetchFailed, InvalidVideoID
from .ytdlp import TrackInfo, download, validate_video_id

_log = logging.getLogger(__name__)

_DB_FILENAME: Final = "index.sqlite"
_AUDIO_SUBDIR: Final = "audio"
_TMP_SUBDIR: Final = "tmp"

# Files younger than this are presumed to belong to a download in
# progress — orphan sweep skips them to dodge the insert/move race.
_ORPHAN_MIN_AGE_S: Final = 3600

# Lavalink's ``LocalAudioSourceManager`` sniffs the file body, so the
# on-disk suffix is decorative — we use a single constant rather than
# magic-byte detection (yt-dlp doesn't surface ext through PR3a's API,
# and the sniff added complexity without a downstream consumer).
_AUDIO_EXT: Final = "audio"

_SCHEMA: Final = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS entries (
  video_id TEXT PRIMARY KEY,
  rel_path TEXT NOT NULL,
  ext TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  title TEXT NOT NULL,
  uploader TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  fetched_at INTEGER NOT NULL,
  last_used_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_lru ON entries(last_used_ts);
"""


def _audio_path(cache_root: Path, video_id: str, ext: str) -> Path:
    """Build the on-disk audio path for ``video_id`` and verify it stays in-bounds.

    ``video_id`` is validated up front; the post-construction
    ``relative_to`` check is defense-in-depth against future
    refactors that might widen the charset.
    """
    validate_video_id(video_id)
    rel = Path(_AUDIO_SUBDIR) / video_id[:2] / f"{video_id}.{ext}"
    final = cache_root / rel
    try:
        final.resolve().relative_to(cache_root.resolve())
    except ValueError as exc:
        raise InvalidVideoID(f"audio path {final!s} escapes cache_root {cache_root!s}") from exc
    return final


class AudioCache:
    """SQLite-backed LRU audio cache with per-video download deduplication.

    Lifecycle: :meth:`open` once at bot startup, :meth:`close` at
    shutdown. The instance is shared across all guilds; per-video
    ``asyncio.Lock`` ensures duplicate ``/play`` requests collapse to a
    single download, and the in-use :class:`~collections.Counter`
    pins active files against eviction.
    """

    def __init__(self, cache_root: Path, max_bytes: int) -> None:
        self._cache_root = cache_root
        self._max_bytes = max_bytes
        self._db_path = cache_root / _DB_FILENAME
        self._audio_root = cache_root / _AUDIO_SUBDIR
        self._tmp_root = cache_root / _TMP_SUBDIR
        # Locks intentionally leaked: per ``M1-simplify.md`` §10 the
        # bytes are trivial vs. the bookkeeping cost of safe eviction.
        self._locks: dict[str, asyncio.Lock] = {}
        # Counter (not set) because the same video may be queued
        # multiple times in a single guild (review §4).
        self._in_use: Counter[str] = Counter()
        self._conn: aiosqlite.Connection | None = None

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    async def open(self) -> None:
        """Initialize directories, sqlite connection, and schema."""
        # Reject a symlink AT cache_root: the in-cache ``relative_to``
        # check defends paths *inside* the cache, but if the root itself
        # is attacker-planted the bot's writes land at the symlink
        # target. Co-resident threat model; cheap defense-in-depth.
        if self._cache_root.exists() and self._cache_root.is_symlink():
            raise RuntimeError(f"cache_root {self._cache_root!s} must not be a symlink")
        for d in (self._cache_root, self._audio_root, self._tmp_root):
            d.mkdir(parents=True, exist_ok=True)
        if not self._cache_root.is_dir():
            raise RuntimeError(f"cache_root {self._cache_root!s} is not a directory")
        self._conn = await aiosqlite.connect(self._db_path)
        # WAL+NORMAL keep readers (LRU touches, eviction scans) from
        # blocking writers (inserts) under concurrent ``/play`` load —
        # review §4. PRAGMAs live in ``_SCHEMA`` so one ``executescript``
        # puts the database into the desired startup state.
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        _log.info("audio cache opened: root=%s max_bytes=%d", self._cache_root, self._max_bytes)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("AudioCache.open() was not called")
        return self._conn

    async def get_or_download(self, track: TrackInfo) -> Path:
        """Return the cached file path for ``track``, downloading if missing.

        Caller MUST :meth:`release` exactly once per call (including on
        Lavalink load failure) or eviction will be permanently skipped
        for that file.
        """
        validate_video_id(track.video_id)

        hit = await self._fast_lookup(track.video_id)
        if hit is not None:
            return await self._finalize_hit(track.video_id, hit)

        lock = self._locks.setdefault(track.video_id, asyncio.Lock())
        async with lock:
            # Double-checked locking — another coroutine may have
            # finished the download while we waited on the lock.
            hit = await self._fast_lookup(track.video_id)
            if hit is not None:
                return await self._finalize_hit(track.video_id, hit)
            return await self._download_locked(track)

    async def _finalize_hit(self, video_id: str, path: Path) -> Path:
        """On a confirmed cache hit: pin BEFORE touch, then return.

        ``_in_use[vid] += 1`` is sync (cannot yield); ``_touch`` is an
        ``await`` that yields the loop. Pinning first prevents a
        concurrent eviction running between ``_fast_lookup`` and the
        pin from deleting our file (symmetric to the slow-path race
        fix in ``_download_locked``; review §4).
        """
        self._in_use[video_id] += 1
        await self._touch(video_id)
        return path

    async def _download_locked(self, track: TrackInfo) -> Path:
        tmp_path = self._tmp_root / f"{track.video_id}.partial"
        try:
            await download(track.url, tmp_path, cache_root=self._cache_root)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if not tmp_path.exists():
            raise FetchFailed(f"download finished but {tmp_path!s} is missing")

        final = _audio_path(self._cache_root, track.video_id, _AUDIO_EXT)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final)  # atomic on the same FS — no partial reads
        # ``os.replace`` preserves the source mtime; refresh it so the
        # orphan sweep's ``mtime > 1h`` guard cannot delete a slow
        # download that just landed (security review LOW-3).
        os.utime(final, None)

        size = final.stat().st_size
        rel_path = str(final.relative_to(self._cache_root))
        now = int(time.time())

        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO entries "
            "(video_id, rel_path, ext, bytes, title, uploader, duration_ms, "
            "fetched_at, last_used_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                track.video_id,
                rel_path,
                _AUDIO_EXT,
                size,
                track.title,
                track.uploader,
                track.duration_ms,
                now,
                now,
            ),
        )
        await conn.commit()

        # Pin BEFORE evicting (review §4 race): otherwise the brand-new
        # row is the LRU oldest with refcount zero, and the evictor we
        # are about to run can delete the file we just downloaded.
        self._in_use[track.video_id] += 1
        await self._evict_to_fit()
        return final

    async def release(self, video_id: str) -> None:
        """Drop one in-use reference; remove the key when it hits zero.

        Called by the Lavalink ``TrackEndEvent`` handler in PR5/PR6a,
        and ALSO by ``/play`` on Lavalink load failure — otherwise
        the refcount leaks and eviction is permanently skipped.
        """
        if self._in_use[video_id] <= 0:
            # Defensive: a double-release after the entry already hit
            # zero (and was popped). Counter[key] returns 0 by default
            # rather than raising; we simply ignore.
            self._in_use.pop(video_id, None)
            return
        self._in_use[video_id] -= 1
        if self._in_use[video_id] <= 0:
            del self._in_use[video_id]

    async def _fast_lookup(self, video_id: str) -> Path | None:
        """Return the on-disk path if a usable entry exists, else None.

        Does NOT mutate. A stale row whose file vanished is treated as
        a miss; the lock-protected branch repairs by INSERT OR REPLACE
        after the fresh download — deleting here would race a concurrent
        download's INSERT and leave a referenced file orphaned in sqlite.
        """
        conn = self._require_conn()
        async with conn.execute(
            "SELECT rel_path FROM entries WHERE video_id = ?", (video_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        path = self._cache_root / row[0]
        if not path.exists():
            return None
        return path

    async def _touch(self, video_id: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "UPDATE entries SET last_used_ts = ? WHERE video_id = ?",
            (int(time.time()), video_id),
        )
        await conn.commit()

    async def _evict_to_fit(self) -> None:
        """Delete oldest non-pinned entries until total bytes ≤ ``max_bytes``."""
        conn = self._require_conn()
        async with conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM entries") as cur:
            total_row = await cur.fetchone()
        total = int(total_row[0]) if total_row else 0
        if total <= self._max_bytes:
            return

        async with conn.execute(
            "SELECT video_id, rel_path, bytes FROM entries ORDER BY last_used_ts ASC"
        ) as cur:
            candidates = await cur.fetchall()

        for video_id, rel_path, size in candidates:
            if total <= self._max_bytes:
                break
            if self._in_use.get(video_id, 0) > 0:
                continue
            file_path = self._cache_root / rel_path
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                _log.warning("eviction failed to unlink %s", file_path, exc_info=True)
                continue
            await conn.execute("DELETE FROM entries WHERE video_id = ?", (video_id,))
            # Commit per iteration so a crash mid-loop cannot leave
            # files unlinked but DELETEs un-flushed (those rolled-back
            # rows would inflate SUM in the next eviction's capacity
            # check). Cheap under WAL+NORMAL.
            await conn.commit()
            total -= int(size)


async def sweep_orphans(cache_root: Path) -> int:
    """Delete tracked-but-unreferenced audio files and stale tmp files older than 1h.

    Walks ``audio/`` and unlinks any file whose ``video_id`` has no
    row in the index AND whose ``mtime`` is older than
    :data:`_ORPHAN_MIN_AGE_S`; also walks ``tmp/`` to reap partial
    files left by crashed downloads. The age guard avoids racing with
    a concurrent download that has not yet inserted its row (review
    §4). Returns the count deleted.
    """
    audio_root = cache_root / _AUDIO_SUBDIR
    tmp_root = cache_root / _TMP_SUBDIR

    db_path = cache_root / _DB_FILENAME
    tracked: set[str] = set()
    if db_path.exists():
        # Sweep runs at startup independently of any AudioCache instance;
        # open our own short-lived connection.
        conn = await aiosqlite.connect(db_path)
        try:
            async with conn.execute("SELECT video_id FROM entries") as cur:
                async for row in cur:
                    tracked.add(row[0])
        finally:
            await conn.close()

    cutoff = time.time() - _ORPHAN_MIN_AGE_S
    deleted = 0

    if audio_root.exists():
        for file_path in audio_root.rglob("*"):
            if not file_path.is_file():
                continue
            video_id = file_path.stem
            if video_id in tracked:
                continue
            if file_path.stat().st_mtime > cutoff:
                continue
            try:
                file_path.unlink()
                deleted += 1
            except OSError:
                _log.warning("orphan sweep failed to unlink %s", file_path, exc_info=True)

    # Tmp files belong to no one once they age past the download
    # window — partial files from crashed downloads accumulate here
    # otherwise (security review LOW-7).
    if tmp_root.exists():
        for file_path in tmp_root.iterdir():
            if not file_path.is_file():
                continue
            if file_path.stat().st_mtime > cutoff:
                continue
            try:
                file_path.unlink()
                deleted += 1
            except OSError:
                _log.warning("orphan sweep failed to unlink %s", file_path, exc_info=True)

    if deleted:
        _log.info("orphan sweep removed %d files under %s", deleted, cache_root)
    return deleted
