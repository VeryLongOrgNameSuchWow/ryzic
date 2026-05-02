"""Embed builders + safe-formatting helpers for slash command responses.

Per M1 §3, every embed must defend against two failure modes:

1. **Markdown injection.** Track titles and uploader names come from
   yt-dlp; a hostile string can include ``**bold**``, ``[anchor](url)``,
   or backtick fences that derail the embed format. :func:`escape_markdown`
   backslash-escapes the Discord-relevant control chars before any string
   is interpolated into a markdown template.

2. **Discord length limits.** Description ≤ 4096, footer ≤ 2048,
   field ≤ 1024. :func:`safe_truncate` clips at a code-point boundary
   (``str`` slicing is already code-point safe in Python 3) and appends
   ``…`` so users see that something was cut.

The builders deliberately produce *unmodified* :class:`hikari.Embed`
instances rather than dict payloads — the caller forwards them to
:meth:`lightbulb.Context.respond` directly. Strings that don't appear in
an embed (the one-shot ephemerals like ``"Join a voice channel first."``)
live at the call site for locality of reference (M1 §3 cross-cutting).

The ``track_info`` accessors (:func:`attach_track_info`, :func:`get_track_info`)
piggyback the original yt-dlp :class:`TrackInfo` on each :class:`lavalink.AudioTrack`
via its ``extra`` dict so commands like ``/queue`` and ``/skip`` can render
the original YouTube URL/title/uploader without re-resolving via yt-dlp.
Lavalink's local source manager surfaces the file path in ``AudioTrack.uri``,
which is unhelpful for embeds.
"""

from __future__ import annotations

from typing import Final

import hikari
import lavalink

from .ytdlp import PlaylistInfo, TrackInfo

# Discord embed limits. Description and footer caps come from
# https://discord.com/developers/docs/resources/message#embed-object-embed-limits.
EMBED_DESCRIPTION_MAX: Final = 4096
EMBED_FOOTER_MAX: Final = 2048
EMBED_FIELD_VALUE_MAX: Final = 1024
EMBED_TITLE_MAX: Final = 256

# Backslash-escape every Discord markdown control char so user-supplied
# strings cannot break out of templating. The ``\`` itself is escaped
# first to avoid double-escaping characters added afterward.
_MARKDOWN_CHARS: Final = ("\\", "[", "]", "(", ")", "*", "_", "~", "`", "|", ">")

# Single ellipsis char rather than three dots — cheaper byte cost vs the
# 4096-char ceiling.
_ELLIPSIS: Final = "…"

# Number of queued entries to enumerate inline in ``/queue``; the rest
# collapse to a single "… and N more" line per M1 §3.
_QUEUE_PREVIEW_MAX: Final = 10

# Key used to stash the original :class:`TrackInfo` on
# :attr:`lavalink.AudioTrack.extra`. Namespaced so a future plugin
# stashing ``track_info`` cannot collide with us.
_TRACK_INFO_EXTRA_KEY: Final = "ryzic_track_info"


def escape_markdown(s: str) -> str:
    """Backslash-escape Discord markdown control chars in ``s``.

    Covers ``\\ [ ] ( ) * _ ~ ` | >``. Block-quote ``>`` is included
    because it activates at line start, and titles can contain newlines.
    """
    out = s
    for ch in _MARKDOWN_CHARS:
        out = out.replace(ch, "\\" + ch)
    return out


def safe_truncate(s: str, max_chars: int) -> str:
    """Truncate ``s`` to at most ``max_chars`` code points, appending ``…`` if cut.

    ``max_chars`` must allow at least one character for the ellipsis; a
    nonpositive value returns the empty string. Python ``str`` slicing
    operates on Unicode code points, so multi-byte characters are not
    split mid-sequence.
    """
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    if max_chars == 1:
        return _ELLIPSIS
    return s[: max_chars - 1] + _ELLIPSIS


def format_duration(ms: int) -> str:
    """Format ``ms`` as ``"M:SS"`` or ``"H:MM:SS"`` for h ≥ 1.

    Negative durations are clamped to zero — Lavalink occasionally
    surfaces negative positions during a seek/replace race.
    """
    if ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def build_queued_track_embed(track: TrackInfo, position: int, *, playing_now: bool) -> hikari.Embed:
    """Build the embed for a single-track ``/play`` success (M1 §3).

    ``position`` is 1-indexed within the queue. ``playing_now`` swaps
    the trailing ``"position N in queue"`` for ``"playing now"`` when
    the queue was empty and the player was idle.
    """
    title = safe_truncate(escape_markdown(track.title), EMBED_DESCRIPTION_MAX // 2)
    uploader = safe_truncate(escape_markdown(track.uploader), EMBED_FOOTER_MAX // 4)
    description = safe_truncate(f"[**{title}**]({track.url})", EMBED_DESCRIPTION_MAX)
    duration = format_duration(track.duration_ms)
    if playing_now:
        footer = f"by {uploader} · {duration} · playing now"
    else:
        footer = f"by {uploader} · {duration} · position {position} in queue"
    embed = hikari.Embed(title="Queued", description=description)
    embed.set_footer(safe_truncate(footer, EMBED_FOOTER_MAX))
    return embed


def build_queued_playlist_embed(
    playlist: PlaylistInfo,
    requester: str,
    *,
    used_cache: bool,
    fetched_at: int | None,
    cache_is_stale: bool,
    failed_count: int = 0,
) -> hikari.Embed:
    """Build the embed for a playlist ``/play`` success (M1 §3).

    ``used_cache`` switches to the "offline metadata" title and footer.
    ``cache_is_stale`` only matters when ``used_cache`` is True; it
    upgrades the warning message to mention the snapshot's age.
    ``fetched_at`` is required when ``used_cache`` is True so the footer
    can render a timestamp the user can act on.

    ``failed_count`` appends the partial-failure footer line per M1 §3
    when some entries couldn't be loaded; the offline-metadata fallback
    already implies this so the line is suppressed there.
    """
    title = safe_truncate(escape_markdown(playlist.title), EMBED_TITLE_MAX)
    track_count = len(playlist.entries)
    total_ms = sum(track.duration_ms for track in playlist.entries)
    duration = format_duration(total_ms)
    description = safe_truncate(
        f"**{title}** — {track_count} tracks ({duration})",
        EMBED_DESCRIPTION_MAX,
    )
    if used_cache:
        embed_title = "Queued playlist (offline metadata)"
        when = "earlier" if fetched_at is None else _format_timestamp(fetched_at)
        # Stale snapshots get a slightly louder mention so users notice
        # entries may have rotated; the fallback path itself is the same.
        suffix = " (snapshot is over 24h old)" if cache_is_stale else ""
        footer = (
            f"yt-dlp could not refresh; using cache from {when}{suffix}. "
            f"Tracks may fail individually."
        )
    else:
        embed_title = "Queued playlist"
        footer = f"requested by {requester}"
        if failed_count > 0:
            footer = f"{footer} · {failed_count} tracks could not be loaded"
    embed = hikari.Embed(title=embed_title, description=description)
    embed.set_footer(safe_truncate(footer, EMBED_FOOTER_MAX))
    return embed


def _format_timestamp(unix_seconds: int) -> str:
    """Format ``unix_seconds`` as a Discord timestamp tag.

    Discord renders ``<t:1234567890:R>`` as a relative timestamp
    ("3 hours ago") localised to the viewer's timezone — far better than
    a server-side ``strftime`` we'd have to apologise for.
    """
    return f"<t:{unix_seconds}:R>"


def attach_track_info(track: lavalink.AudioTrack, info: TrackInfo) -> None:
    """Stash ``info`` on ``track.extra`` for later embed rendering.

    The local source manager populates ``AudioTrack.title``/``uri`` from
    the file we hand it (e.g. ``/var/cache/ryzic/audio/.../dQw4w9.audio``)
    so they are unusable for user-facing display. Callers (``/play``)
    invoke this immediately before ``player.add`` so every queued
    track carries its original yt-dlp metadata.
    """
    track.extra[_TRACK_INFO_EXTRA_KEY] = info


def get_track_info(track: lavalink.AudioTrack) -> TrackInfo | None:
    """Return the original :class:`TrackInfo` for ``track`` if attached.

    Returns ``None`` when the track was enqueued without metadata
    (e.g. surfaced by a future code path that bypasses
    :func:`attach_track_info`); callers must handle the missing case
    gracefully rather than KeyError.
    """
    info = track.extra.get(_TRACK_INFO_EXTRA_KEY)
    return info if isinstance(info, TrackInfo) else None


def build_queue_embed(
    *,
    now_playing: TrackInfo,
    now_playing_position_ms: int,
    paused: bool,
    queue: list[tuple[TrackInfo, int]],
) -> hikari.Embed:
    """Build the ``/queue`` embed (M1 §3).

    ``queue`` is a list of ``(track_info, requester_id)`` tuples in
    queue order — the entry at index 0 plays next. The embed enumerates
    the first :data:`_QUEUE_PREVIEW_MAX` entries inline; anything beyond
    collapses to ``"… and N more"`` so we never blow the 4096-char
    description budget on long playlists.
    """
    queue_count = len(queue)
    queue_total_ms = sum(info.duration_ms for info, _ in queue)
    title = f"Queue ({queue_count} tracks · {format_duration(queue_total_ms)})"

    np_title = safe_truncate(escape_markdown(now_playing.title), EMBED_FIELD_VALUE_MAX // 2)
    progress = (
        f"{format_duration(now_playing_position_ms)} / {format_duration(now_playing.duration_ms)}"
    )
    if paused:
        progress = f"{progress} (paused)"
    now_playing_value = safe_truncate(
        f"[**{np_title}**]({now_playing.url})\n{progress}",
        EMBED_FIELD_VALUE_MAX,
    )

    description = _build_queue_description(queue)

    embed = hikari.Embed(title=title, description=description)
    embed.add_field(name="Now playing", value=now_playing_value, inline=False)
    return embed


def _build_queue_description(queue: list[tuple[TrackInfo, int]]) -> str:
    """Format the description body of the ``/queue`` embed.

    Returns the empty string when ``queue`` is empty so the caller's
    embed has no description (the Now playing field stands alone).
    """
    if not queue:
        return ""
    preview = queue[:_QUEUE_PREVIEW_MAX]
    lines = [
        (
            f"{idx}. [{escape_markdown(info.title)}]({info.url}) — "
            f"{format_duration(info.duration_ms)} (req. by <@{requester_id}>)"
        )
        for idx, (info, requester_id) in enumerate(preview, start=1)
    ]
    overflow = len(queue) - len(preview)
    if overflow > 0:
        lines.append(f"… and {overflow} more")
    return safe_truncate("\n".join(lines), EMBED_DESCRIPTION_MAX)
