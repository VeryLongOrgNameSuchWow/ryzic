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
from collections.abc import Iterable
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

# Default extension when content-sniffing finds nothing recognizable.
# Lavalink's ``LocalAudioSourceManager`` sniffs the file body, so the
# on-disk suffix is decorative; we still want a consistent value for
# the sqlite ``ext`` column and easier debugging.
_FALLBACK_EXT: Final = "audio"

_SCHEMA: Final = """
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


def _detect_ext(path: Path) -> str:
    """Return the storage extension for ``path`` based on a tiny magic-byte sniff.

    The format selector in :mod:`ryzic.ytdlp` prefers m4a → opus →
    webm → bestaudio. Detecting the actual container lets the on-disk
    layout match the codec for debugging without relying on yt-dlp
    surfacing the ext through PR3a's narrow API.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return _FALLBACK_EXT
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"OggS"):
        return "opus"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return _FALLBACK_EXT


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
        for d in (self._cache_root, self._audio_root, self._tmp_root):
            d.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        # WAL keeps readers (LRU touches, eviction scans) from blocking
        # writers (inserts) under concurrent ``/play`` load — review §4.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
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

        Pins the entry via ``_in_use[video_id] += 1`` BEFORE returning
        — caller MUST :meth:`release` exactly once per call (including
        on Lavalink load failure) or eviction will be permanently
        skipped for that file.
        """
        validate_video_id(track.video_id)

        hit = await self._fast_lookup(track.video_id)
        if hit is not None:
            await self._touch(track.video_id)
            self._in_use[track.video_id] += 1
            return hit

        lock = self._locks.setdefault(track.video_id, asyncio.Lock())
        async with lock:
            # Double-checked locking — another coroutine may have
            # finished the download while we waited on the lock.
            hit = await self._fast_lookup(track.video_id)
            if hit is not None:
                await self._touch(track.video_id)
                self._in_use[track.video_id] += 1
                return hit
            return await self._download_locked(track)

    async def _download_locked(self, track: TrackInfo) -> Path:
        tmp_path = self._tmp_root / f"{track.video_id}.partial"
        try:
            await download(track.url, tmp_path, cache_root=self._cache_root)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if not tmp_path.exists():
            raise FetchFailed(f"download finished but {tmp_path!s} is missing")

        ext = _detect_ext(tmp_path)
        final = _audio_path(self._cache_root, track.video_id, ext)
        final.parent.mkdir(parents=True, exist_ok=True)
        # ``os.replace`` is atomic on the same filesystem — readers
        # never see a partial file at ``final``.
        os.replace(tmp_path, final)

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
                ext,
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

    async def release_many(self, video_ids: Iterable[str]) -> None:
        """Drop one reference per id — for ``WebSocketClosedEvent`` / ``QueueEndEvent``.

        Caller (PR5/PR6a) iterates the guild's queue once and passes
        every video_id; the cache stays guild-agnostic.
        """
        for vid in video_ids:
            await self.release(vid)

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
            total -= int(size)
        await conn.commit()


async def sweep_orphans(cache_root: Path) -> int:
    """Delete tracked-but-unreferenced audio files older than 1h.

    Walks the ``audio/`` tree and unlinks any file whose ``video_id``
    has no row in the index AND whose ``mtime`` is older than
    :data:`_ORPHAN_MIN_AGE_S` — the age guard avoids racing with a
    concurrent download that has not yet inserted its row (review
    §4). Returns the count deleted, primarily for tests.
    """
    audio_root = cache_root / _AUDIO_SUBDIR
    if not audio_root.exists():
        return 0

    db_path = cache_root / _DB_FILENAME
    tracked: set[str] = set()
    if db_path.exists():
        # Open a separate short-lived connection: the sweep is invoked
        # at startup independently of :class:`AudioCache`, and SQLite
        # WAL allows concurrent readers without contention.
        conn = await aiosqlite.connect(db_path)
        try:
            async with conn.execute("SELECT video_id FROM entries") as cur:
                async for row in cur:
                    tracked.add(row[0])
        finally:
            await conn.close()

    cutoff = time.time() - _ORPHAN_MIN_AGE_S
    deleted = 0
    for file_path in audio_root.rglob("*"):
        if not file_path.is_file():
            continue
        video_id = file_path.stem
        if video_id in tracked:
            continue
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue
        try:
            file_path.unlink()
            deleted += 1
        except OSError:
            _log.warning("orphan sweep failed to unlink %s", file_path, exc_info=True)
    if deleted:
        _log.info("orphan sweep removed %d files under %s", deleted, audio_root)
    return deleted
