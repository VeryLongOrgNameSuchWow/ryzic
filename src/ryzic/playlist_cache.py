"""Playlist metadata cache (per M1 §5).

Stores resolved :class:`~ryzic.ytdlp.PlaylistInfo` snapshots as JSON
files under ``{cache_root}/playlists/{playlist_id}.json``. The cache
exists for one job: keep ``/play <playlist_url>`` working when yt-dlp
breaks. Per spec, the lookup flow is **live-first, cache as fallback** —
:func:`fetch_with_fallback` always tries yt-dlp first and only reads the
cache when the live call raises.

TTL is hardcoded at 24h per ``M1-simplify.md`` §3 — the value gates the
embed's "stale data" warning, not the fallback decision itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from .errors import FetchFailed, InvalidVideoID
from .ytdlp import PlaylistInfo, TrackInfo, resolve_playlist

_log = logging.getLogger(__name__)

# YouTube playlist IDs are typically 13-34 chars; bound generously while
# constraining the charset to the same path-safe alphabet used for video
# IDs in :mod:`ryzic.ytdlp`. Always validated with ``fullmatch`` so a
# trailing ``\n`` cannot satisfy ``$`` and slip a control character into
# a filename or log line; runs BEFORE any path join so an adversarial id
# like ``../../etc`` cannot escape ``cache_root``.
_PLAYLIST_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{10,50}")

_TTL_SECONDS: Final = 24 * 60 * 60

_PLAYLISTS_DIR: Final = "playlists"


def _validate_playlist_id(playlist_id: str) -> None:
    # ``fullmatch`` (not ``match``) so a trailing ``\n`` cannot satisfy
    # ``$`` and slip a control character into the on-disk filename or a
    # log line — Python's default-mode ``$`` matches before a final
    # newline.
    if not _PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise InvalidVideoID(f"playlist_id failed validation: {playlist_id!r}")


def _path_for(playlist_id: str, cache_root: Path) -> Path:
    _validate_playlist_id(playlist_id)
    derived = cache_root / _PLAYLISTS_DIR / f"{playlist_id}.json"
    # Belt-and-braces: if the regex ever loosens, the resolved path must
    # still live under ``cache_root``.
    try:
        derived.resolve().relative_to(cache_root.resolve())
    except ValueError as exc:
        raise InvalidVideoID(f"playlist_id resolves outside cache_root: {playlist_id!r}") from exc
    return derived


def _serialize(info: PlaylistInfo, fetched_at: int) -> dict[str, Any]:
    return {
        "playlist_id": info.playlist_id,
        "title": info.title,
        "fetched_at": fetched_at,
        "entries": [asdict(track) for track in info.entries],
    }


def _deserialize(payload: dict[str, Any]) -> PlaylistInfo:
    """Rebuild a :class:`PlaylistInfo` from a cache file.

    Raises :class:`ValueError`/:class:`KeyError`/:class:`TypeError` for
    any structural mismatch — the caller treats a malformed file as a
    cache miss rather than crashing.
    """
    playlist_id = payload["playlist_id"]
    title = payload["title"]
    raw_entries = payload["entries"]
    if not isinstance(playlist_id, str) or not isinstance(title, str):
        raise ValueError("playlist_id/title must be strings")
    if not isinstance(raw_entries, list):
        raise ValueError("entries must be a list")
    entries = [
        TrackInfo(
            video_id=str(e["video_id"]),
            url=str(e["url"]),
            title=str(e["title"]),
            uploader=str(e["uploader"]),
            duration_ms=int(e["duration_ms"]),
        )
        for e in raw_entries
    ]
    return PlaylistInfo(playlist_id=playlist_id, title=title, entries=entries)


def _read_sync(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(text)


def _write_sync(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace ``path`` with the JSON-encoded ``payload``.

    Writes to a sibling tempfile then ``os.replace`` to avoid a torn
    file on crash mid-write — readers either see the previous version
    or the new one, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def read(playlist_id: str, cache_root: Path) -> tuple[PlaylistInfo, int] | None:
    """Return the cached ``(info, fetched_at)`` for ``playlist_id``, or ``None``.

    Returns ``None`` for misses AND for malformed cache files; the
    fallback path treats both identically. Surfacing ``fetched_at``
    alongside ``info`` lets the caller answer "is this stale?" without a
    second disk read on the event loop.
    """
    path = _path_for(playlist_id, cache_root)
    try:
        payload = await asyncio.to_thread(_read_sync, path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _log.warning("dropping unreadable playlist cache entry: %s", path)
        return None
    if payload is None:
        return None
    try:
        info = _deserialize(payload)
    except (ValueError, KeyError, TypeError):
        _log.warning("dropping malformed playlist cache entry: %s", path)
        return None
    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, int):
        _log.warning("dropping malformed playlist cache entry: %s", path)
        return None
    return info, fetched_at


async def write(
    playlist_id: str,
    info: PlaylistInfo,
    cache_root: Path,
    *,
    fetched_at: int | None = None,
) -> int:
    """Persist ``info`` to the cache under ``playlist_id``.

    The on-disk ``playlist_id`` MUST equal the function arg — the path is
    derived from the arg, so a mismatched payload would cache the wrong
    file name and silently break the next read.

    Returns the timestamp written so callers can pass the same value
    back to :func:`is_stale` without re-reading the file.
    """
    if info.playlist_id != playlist_id:
        raise InvalidVideoID(f"playlist_id mismatch: arg={playlist_id!r} info={info.playlist_id!r}")
    path = _path_for(playlist_id, cache_root)
    if fetched_at is None:
        fetched_at = int(time.time())
    payload = _serialize(info, fetched_at=fetched_at)
    await asyncio.to_thread(_write_sync, path, payload)
    return fetched_at


def is_stale(fetched_at: int) -> bool:
    """Return True iff ``fetched_at`` is older than 24h relative to now."""
    return (time.time() - fetched_at) > _TTL_SECONDS


def _extract_playlist_id(url: str) -> str | None:
    """Pull the first ``list=`` query param from a YouTube URL, or ``None``.

    When ``list=`` repeats, ``parse_qs`` returns all values; we pick the
    first deterministically. If that first value fails validation, the
    function returns ``None`` rather than scanning later values — every
    cache lookup must agree on a single canonical id per URL.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    values = parse_qs(parsed.query).get("list")
    if not values:
        return None
    candidate = values[0]
    return candidate if _PLAYLIST_ID_RE.fullmatch(candidate) else None


async def fetch_with_fallback(url: str, *, cache_root: Path) -> tuple[PlaylistInfo, int, bool]:
    """Resolve ``url`` live; on failure, fall back to cached metadata.

    Returns ``(info, fetched_at, used_cache_fallback)``. The bool tells
    callers (e.g. ``/play``) to attach the "offline metadata" footer;
    pass ``fetched_at`` to :func:`is_stale` to drive the staleness
    warning. On a live success ``fetched_at`` is the just-now timestamp
    persisted to disk.

    Raises the original :class:`FetchFailed` if BOTH yt-dlp and the
    cache fail — "unsinkable when yt-dlp breaks" only applies when we
    have something to fall back to.
    """
    try:
        info = await resolve_playlist(url, cache_root=cache_root)
    except FetchFailed as exc:
        playlist_id = _extract_playlist_id(url)
        if playlist_id is None:
            raise
        cached = await read(playlist_id, cache_root)
        if cached is None:
            raise
        cached_info, fetched_at = cached
        _log.warning(
            "yt-dlp failed for playlist %s; serving cached metadata: %s",
            playlist_id,
            exc,
        )
        return cached_info, fetched_at, True
    fetched_at = await write(info.playlist_id, info, cache_root)
    return info, fetched_at, False
