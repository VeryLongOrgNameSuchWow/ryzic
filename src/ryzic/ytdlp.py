"""Async wrapper around yt-dlp's embedded Python API (per M1 §6).

Sync ``YoutubeDL`` calls are dispatched to a worker thread via
:func:`asyncio.to_thread` so the event loop stays unblocked. The
embedded API is used exclusively; we never spawn a subprocess and never
invoke a shell.

Errors are normalized to :class:`~ryzic.errors.FetchFailed` with a
short, user-presentable sentence (per M1 §3) ready for ``/play`` to
display verbatim. Full tracebacks for unexpected failures are logged at
``ERROR``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError

from .errors import FetchFailed, InvalidVideoID
from .url_validator import is_supported_url

_log = logging.getLogger(__name__)

_VIDEO_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# Live status sentinels yt-dlp surfaces for active or scheduled streams.
# Recordings (``"was_live"``, ``"post_live"``) are downloadable VODs
# and intentionally excluded.
_LIVE_STATUSES: Final = frozenset({"is_live", "is_upcoming"})

# Substring → catalog-key mapping. The first line of yt-dlp's
# ``DownloadError`` is scanned; the matching key feeds ``FetchFailed``.
# Catalog body owns the user-facing copy (per the i18n migration); the
# substrings stay verbatim — they're matched against yt-dlp's raw output.
_FRIENDLY_ERROR_KEYS: Final[dict[str, str]] = {
    "Sign in to confirm your age": "ytdlp.error.age_restricted",
    "Private video": "ytdlp.error.private",
    "Video unavailable": "ytdlp.error.region_blocked",
    # ``resolve_track`` / ``resolve_playlist`` skip format selection
    # (``process=False``); livestreams are caught by ``_is_livestream``.
    # This now surfaces only from ``download()`` — a real codec problem.
    "Requested format is not available": "ytdlp.error.format_unavailable",
}

# Cap and scrub yt-dlp error fragments before they surface to users.
# Backticks are stripped so the embed builder can wrap the message in an
# inline code span without breakout. Absolute-looking paths are masked
# so the host's filesystem layout doesn't leak via Discord.
_MAX_ERROR_LEN: Final = 200
# Match absolute-looking paths (Unix and Windows). Negative lookbehind on
# ``:`` and ``/`` keeps URL schemes/hostnames intact while still scrubbing
# the path component.
_PATH_LIKE_RE: Final = re.compile(r"(?<![:/])(?:[A-Za-z]:)?/[A-Za-z0-9_.\\-][A-Za-z0-9_./\\-]*")


class TrackInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    video_id: str
    url: str
    title: str
    uploader: str
    duration_ms: int


class PlaylistInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    playlist_id: str
    title: str
    entries: list[TrackInfo]


def validate_video_id(video_id: str) -> None:
    """Raise :class:`InvalidVideoID` if ``video_id`` is outside the allowed charset/length."""
    if not _VIDEO_ID_RE.match(video_id):
        raise InvalidVideoID(f"video_id failed validation: {video_id!r}")


def parse_video_id(url: str) -> str | None:
    """Extract the YouTube video id from a supported URL, or ``None``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    candidate: str | None = None
    qs = parse_qs(parsed.query)
    v_values = qs.get("v")
    if v_values:
        candidate = v_values[0]
    elif parsed.hostname == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0] or None
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "v"}:
            candidate = parts[1]
    if candidate is None:
        return None
    if not _VIDEO_ID_RE.match(candidate):
        return None
    return candidate


# Module-level singleton: ``bot.py`` reads the opt-in
# ``RYZIC_YOUTUBE_COOKIES_PATH`` env var at startup and installs the
# resolved path here so ``_base_opts`` can fold it into every yt-dlp
# call without threading the config through every caller. Symmetric
# to ``audio_cache.set_audio_cache``. Unset = the safe, cookie-less
# default documented in the README.
_COOKIES_PATH: Path | None = None


def set_cookies_path(path: Path | None) -> None:
    """Install (or clear) the opt-in YouTube cookies file path.

    When set, every subsequent yt-dlp invocation passes ``cookiefile``
    pointing at ``path``. When ``None`` (the default), no cookies are
    sent — preserving the security posture documented in the README.

    SECURITY: enabling this lets any user who can run a slash command
    fetch any video the cookies' YouTube account can see (private,
    age-restricted, Premium-only). See the README's "Self-hoster
    considerations" section before installing a non-``None`` path.
    """
    global _COOKIES_PATH
    _COOKIES_PATH = path


def _base_opts() -> dict[str, Any]:
    """Build the frozen yt-dlp options dict (per M1 §6).

    Format priority constrains output to known-good Lavaplayer codecs
    (review §6 LOAD_FAILED on exotic codecs). ``cookiesfrombrowser``
    stays disabled unconditionally (browser extraction is well outside
    the bot's blast radius). ``cookiefile`` is opt-in via
    :func:`set_cookies_path`; absent by default.
    """
    opts: dict[str, Any] = {
        "format": ("bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio"),
        "noplaylist": True,
        "extract_flat": False,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 1,
        "geo_bypass": False,
        "max_filesize": 500_000_000,
        "playlist_items": "1-1000",
        # SECURITY: browser-extracted cookies stay unconditionally
        # disabled. The file-based opt-in below is the only supported
        # path; browser extraction would imply scanning the host's
        # profile dirs, well outside the bot's blast radius.
        "cookiesfrombrowser": None,
        # SECURITY: disable yt-dlp's plugin auto-loader. The default
        # (``['default']``) scans config dirs and ``sys.path`` for
        # namespace packages named ``yt_dlp_plugins`` and executes
        # them in-process. An empty list disables the entire mechanism.
        "plugin_dirs": [],
        # SECURITY: pin the extractor set to YouTube. The hostname
        # allowlist is the primary defense; this is a second wall against
        # the ``Generic`` extractor probing arbitrary HTML if a future
        # yt-dlp release reorders match precedence. ``youtube`` covers
        # watch/youtu.be URLs; ``youtube:tab`` covers playlists.
        "allowed_extractors": ["youtube", "youtube:tab"],
    }
    if _COOKIES_PATH is not None:
        # Opt-in only; unset = no key at all, preserving the
        # cookie-less default. yt-dlp owns format validation.
        opts["cookiefile"] = str(_COOKIES_PATH)
    return opts


def _is_livestream(info: dict[str, Any]) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") in _LIVE_STATUSES


def _coerce_duration_ms(raw: Any) -> int:
    """Convert yt-dlp's ``duration`` (seconds, float|int|None) to milliseconds."""
    if raw is None:
        return 0
    return int(float(raw) * 1000)


def _scrub(text: str) -> str:
    """Strip backticks and absolute-looking paths; cap length.

    Defense-in-depth (security review LOW-10): the embed builder will
    further escape for Discord, but cleansing here makes every consumer
    safe-by-default and prevents the host's filesystem layout from
    leaking via yt-dlp error fragments.
    """
    cleaned = _PATH_LIKE_RE.sub("<path>", text).replace("`", "")
    if len(cleaned) > _MAX_ERROR_LEN:
        cleaned = cleaned[: _MAX_ERROR_LEN - 1].rstrip() + "…"
    return cleaned


def _track_from_info(info: dict[str, Any]) -> TrackInfo:
    video_id = info.get("id")
    if not isinstance(video_id, str):
        raise FetchFailed("ytdlp.error.no_video_id")
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


def _sync_extract(
    opts: dict[str, Any], url: str, *, download: bool, process: bool = True
) -> dict[str, Any]:
    """Run ``YoutubeDL.extract_info`` synchronously; return the sanitized info dict."""
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download, process=process)
        if info is None:
            raise FetchFailed("ytdlp.error.no_info")
        # yt-dlp ships no type stubs, so ty sees the return as ``Any``.
        return ydl.sanitize_info(info)  # type: ignore[no-any-return]


async def _extract(
    opts: dict[str, Any],
    url: str,
    *,
    download: bool,
    process: bool = True,
) -> dict[str, Any]:
    """Run ``_sync_extract`` in a worker thread and normalize errors."""
    try:
        return await asyncio.to_thread(_sync_extract, opts, url, download=download, process=process)
    except FetchFailed:
        raise
    except YoutubeDLError as exc:
        # ``DownloadError`` is the common case; widening to the parent
        # catches sibling extractor errors that bypass yt-dlp's normal
        # wrap (e.g. ``GeoRestrictedError`` raised from a custom path).
        detail = next(
            (s for s in (line.strip() for line in str(exc).splitlines()) if s),
            str(exc).strip(),
        )
        key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() if substr in detail), None)
        if key is None:
            raise FetchFailed("ytdlp.error.generic_with_detail", detail=_scrub(detail)) from exc
        raise FetchFailed(key) from exc
    except Exception as exc:
        _log.exception("yt-dlp internal error for url=%s", url)
        raise FetchFailed("ytdlp.error.internal", name=exc.__class__.__name__) from exc


async def resolve_track(url: str, *, cache_root: Path) -> TrackInfo:
    """Resolve a single-video URL to a :class:`TrackInfo`."""
    if not is_supported_url(url):
        raise FetchFailed("ytdlp.error.unsupported_url")
    opts = _base_opts()
    # ``process=False`` skips format selection; the bestaudio fallback chain
    # is irrelevant on info-extract and triggers spurious "Requested format is
    # not available" errors for some VODs (issue #132).
    info = await _extract(opts, url, download=False, process=False)
    if _is_livestream(info):
        raise FetchFailed("ytdlp.error.livestream")
    return _track_from_info(info)


async def resolve_playlist(url: str, *, cache_root: Path) -> PlaylistInfo:
    """Resolve a playlist URL to a :class:`PlaylistInfo` via flat extraction.

    Per M1 §6, flat extraction trades per-entry detail for one round
    trip; individual livestream checks happen later at per-track
    resolution time.
    """
    if not is_supported_url(url):
        raise FetchFailed("ytdlp.error.unsupported_url")
    opts = _base_opts()
    opts["noplaylist"] = False
    opts["extract_flat"] = True
    info = await _extract(opts, url, download=False, process=False)
    playlist_id = info.get("id")
    if not isinstance(playlist_id, str):
        raise FetchFailed("ytdlp.error.no_playlist_id")
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
    yt-dlp write outside the cache.

    Note: a TOCTOU window remains between ``Path.resolve`` and
    yt-dlp's actual ``open()``. The cache directory's permissions
    (0o700, bot-owned) are the deploy-time mitigation; harder
    O_NOFOLLOW-style guards are deferred to the cache subsystem (PR3b).
    """
    if not is_supported_url(url):
        raise FetchFailed("ytdlp.error.unsupported_url")
    resolved_root = cache_root.resolve()
    resolved_dest = dest.resolve()
    try:
        resolved_dest.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidVideoID(f"download dest {dest!s} escapes cache_root {cache_root!s}") from exc

    opts = _base_opts()
    opts["outtmpl"] = str(resolved_dest)
    info = await _extract(opts, url, download=True)
    if _is_livestream(info):
        raise FetchFailed("ytdlp.error.livestream")
