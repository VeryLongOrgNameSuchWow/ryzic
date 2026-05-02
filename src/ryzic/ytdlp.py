"""Async wrapper around yt-dlp's embedded Python API (per M1 §6).

Module functions only — no class. Sync ``YoutubeDL`` calls are dispatched
to a worker thread via :func:`asyncio.to_thread` so the event loop stays
unblocked. The embedded API is used exclusively; we never spawn a
subprocess and never invoke a shell.

Public surface:

* :func:`resolve_track` — metadata for a single video URL.
* :func:`resolve_playlist` — flat metadata listing for a playlist URL.
* :func:`download` — download audio for ``url`` to ``dest`` under ``cache_root``.
* :func:`validate_video_id` — raises :class:`~ryzic.errors.InvalidVideoID`
  for IDs outside the allowed character set / length window. Exposed so
  the cache layer can pre-validate before constructing paths.

Errors are normalized to :class:`~ryzic.errors.FetchFailed` with a short,
user-presentable message; the ``/play`` command remaps known patterns
to friendlier wording. Full tracebacks for unexpected failures are
logged at ``ERROR``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .errors import FetchFailed, InvalidVideoID

_log = logging.getLogger(__name__)

_VIDEO_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# Live status sentinels yt-dlp surfaces for active or scheduled streams.
# Recordings (``"was_live"``, ``"post_live"``) are downloadable VODs
# and intentionally excluded.
_LIVE_STATUSES: Final = frozenset({"is_live", "is_upcoming"})

# Known yt-dlp error fragments mapped to clean, user-presentable
# messages. Matched substring-wise against the first line of
# ``DownloadError.args[0]``. Order doesn't matter — each pattern is
# unique enough to discriminate.
_FRIENDLY_ERROR_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("Sign in to confirm your age", "age-restricted"),
    ("Private video", "private video"),
    ("Video unavailable", "region-blocked or unavailable"),
)


@dataclass(frozen=True)
class TrackInfo:
    video_id: str
    url: str
    title: str
    uploader: str
    duration_ms: int


@dataclass(frozen=True)
class PlaylistInfo:
    playlist_id: str
    title: str
    entries: list[TrackInfo]


def validate_video_id(video_id: str) -> None:
    """Raise :class:`InvalidVideoID` if ``video_id`` is outside the allowed charset/length."""
    if not _VIDEO_ID_RE.match(video_id):
        raise InvalidVideoID(f"video_id failed validation: {video_id!r}")


def _base_opts(cache_root: Path) -> dict[str, Any]:
    """Build the frozen yt-dlp options dict (per M1 §6).

    Format priority constrains output to known-good Lavaplayer codecs
    (review §6 LOAD_FAILED on exotic codecs). ``cookiefile`` MUST stay
    None — see §6 security item 13.
    """
    return {
        "format": ("bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio"),
        "noplaylist": True,
        "extract_flat": False,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "paths": {"home": str(cache_root / "tmp")},
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": False,
        "max_filesize": 500_000_000,
        "playlist_items": "1-1000",
        # SECURITY: cookies are deliberately disabled. Enabling them
        # exposes the host's YouTube session to any URL the bot
        # resolves; out of M1 scope and requires its own security
        # review before flipping.
        "cookiefile": None,
        "logger": _log,
    }


def _first_line(message: str) -> str:
    """Return the first non-empty line of ``message`` (defensively trimmed)."""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return message.strip()


def _map_friendly(detail: str) -> str | None:
    for needle, friendly in _FRIENDLY_ERROR_PATTERNS:
        if needle in detail:
            return friendly
    return None


def _raise_from_download_error(exc: DownloadError) -> None:
    """Translate a yt-dlp ``DownloadError`` into a :class:`FetchFailed`."""
    detail = _first_line(str(exc))
    friendly = _map_friendly(detail)
    raise FetchFailed(friendly or detail) from exc


def _is_livestream(info: dict[str, Any]) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") in _LIVE_STATUSES


def _check_not_livestream(info: dict[str, Any]) -> None:
    if _is_livestream(info):
        raise FetchFailed("livestream")


def _reject_livestream_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
    """yt-dlp ``match_filter`` callback that aborts before any bytes hit disk."""
    if _is_livestream(info):
        return "livestream"
    return None


def _coerce_duration_ms(raw: Any) -> int:
    """Convert yt-dlp's ``duration`` (seconds, float|int|None) to milliseconds."""
    if raw is None:
        return 0
    return int(float(raw) * 1000)


def _track_from_info(info: dict[str, Any]) -> TrackInfo:
    video_id = info.get("id")
    if not isinstance(video_id, str):
        raise FetchFailed("yt-dlp returned no video id")
    validate_video_id(video_id)
    return TrackInfo(
        video_id=video_id,
        url=info.get("webpage_url") or info.get("url") or "",
        title=info.get("title") or "Unknown title",
        uploader=info.get("uploader") or info.get("channel") or "Unknown uploader",
        duration_ms=_coerce_duration_ms(info.get("duration")),
    )


def _entry_from_flat(entry: dict[str, Any]) -> TrackInfo | None:
    """Build a :class:`TrackInfo` from one ``extract_flat`` playlist entry.

    Returns ``None`` if the entry can't be reduced to a usable track
    (private/deleted videos, missing IDs, IDs outside the YouTube
    charset). Per M1 §3, the playlist embed surfaces partial-failure
    counts rather than aborting the whole listing.
    """
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.match(video_id):
        return None
    return TrackInfo(
        video_id=video_id,
        url=entry.get("url") or entry.get("webpage_url") or f"https://youtu.be/{video_id}",
        title=entry.get("title") or "Unknown title",
        uploader=entry.get("uploader") or entry.get("channel") or "Unknown uploader",
        duration_ms=_coerce_duration_ms(entry.get("duration")),
    )


def _sync_extract(opts: dict[str, Any], url: str, *, download: bool) -> dict[str, Any]:
    """Run ``YoutubeDL.extract_info`` synchronously; return the sanitized info dict."""
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download)
        if info is None:
            raise FetchFailed("yt-dlp returned no info")
        return ydl.sanitize_info(info)  # type: ignore[no-any-return]


async def _extract(
    opts: dict[str, Any],
    url: str,
    *,
    download: bool,
) -> dict[str, Any]:
    """Run ``_sync_extract`` in a worker thread and normalize errors."""
    try:
        return await asyncio.to_thread(_sync_extract, opts, url, download=download)
    except FetchFailed:
        raise
    except DownloadError as exc:
        _raise_from_download_error(exc)
        # ``_raise_from_download_error`` always raises; this satisfies
        # static analysis without a noqa.
        raise AssertionError("unreachable") from None  # pragma: no cover
    except Exception as exc:
        _log.exception("yt-dlp internal error for url=%s", url)
        raise FetchFailed(f"internal error: {exc.__class__.__name__}") from exc


async def resolve_track(url: str, *, cache_root: Path) -> TrackInfo:
    """Resolve a single-video URL to a :class:`TrackInfo`.

    Raises :class:`FetchFailed` (with ``"livestream"`` for active/upcoming
    streams) on any yt-dlp failure.
    """
    opts = _base_opts(cache_root)
    info = await _extract(opts, url, download=False)
    _check_not_livestream(info)
    return _track_from_info(info)


async def resolve_playlist(url: str, *, cache_root: Path) -> PlaylistInfo:
    """Resolve a playlist URL to a :class:`PlaylistInfo` via flat extraction.

    Per M1 §6, flat extraction trades per-entry detail for one round
    trip; individual livestream checks happen later at per-track
    resolution time.
    """
    opts = _base_opts(cache_root)
    opts["noplaylist"] = False
    opts["extract_flat"] = True
    info = await _extract(opts, url, download=False)
    playlist_id = info.get("id")
    if not isinstance(playlist_id, str):
        raise FetchFailed("yt-dlp returned no playlist id")
    raw_entries = info.get("entries") or []
    entries = [
        track
        for track in (_entry_from_flat(e) for e in raw_entries if isinstance(e, dict))
        if track is not None
    ]
    return PlaylistInfo(
        playlist_id=playlist_id,
        title=info.get("title") or "Unknown playlist",
        entries=entries,
    )


async def download(url: str, dest: Path, *, cache_root: Path) -> None:
    """Download audio for ``url`` to ``dest`` (must resolve under ``cache_root``).

    The ``dest`` path is sandbox-checked via ``Path.relative_to``; on
    violation we raise :class:`InvalidVideoID` rather than letting
    yt-dlp write outside the cache. Livestreams are rejected before any
    bytes hit disk.
    """
    resolved_root = cache_root.resolve()
    resolved_dest = dest.resolve()
    try:
        resolved_dest.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidVideoID(f"download dest {dest!s} escapes cache_root {cache_root!s}") from exc

    opts = _base_opts(cache_root)
    opts["outtmpl"] = str(resolved_dest)
    opts["match_filter"] = _reject_livestream_filter
    info = await _extract(opts, url, download=True)
    # Defense-in-depth: ``match_filter`` should have aborted, but a
    # post-check costs nothing and keeps the contract explicit.
    _check_not_livestream(info)
