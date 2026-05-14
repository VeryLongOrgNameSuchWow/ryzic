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

**i18n contract.** Every public builder takes a ``locale: str`` keyword
argument (required, no default). Strings live in the catalog at
``src/ryzic/i18n/locales/{locale}.json``; the builder ``t()``-renders
each catalog key at call time. Variables that interpolate inside a
markdown structure (``**%{title}**``, ``[%{label}](%{url})``) are
``escape_markdown``-sanitized at the call site before being passed to
``t()`` — the catalog template owns the markdown wrappers and never sees
unescaped user data.
"""

from __future__ import annotations

import re
from typing import Final

import hikari
import lavalink

from .i18n import t
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

# Number of queued entries per page in ``/queue`` (issue #99). Public
# because the ``/queue`` command reads it to compute ``total_pages``;
# keeping the page-size definition in one place avoids drift between
# the slicing boundary and the user-facing pagination math.
QUEUE_PAGE_SIZE: Final = 10

# Number of history entries enumerated inline in ``/recent``. The ring's
# hard cap (``track_history.MAX_HISTORY_SIZE``) is the upper bound; the
# preview cap exists so a future widening of that constant cannot
# quietly blow Discord's description budget.
_RECENT_PREVIEW_MAX: Final = 25

# Namespaced ``AudioTrack.extra`` key for the stashed :class:`TrackInfo`.
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


# Absolute form: optional ``H:`` prefix + ``M:S`` or ``M:SS``. Per-component
# numeric bounds (``MM < 60``, ``SS < 60``) live in :func:`parse_seek_position`
# so the regex stays focused on shape. Single-digit seconds are intentional
# — Discord mobile makes zero-padding tedious; ``1:5`` is a reasonable thing
# to type and we read it as 1m5s.
_SEEK_ABSOLUTE_RE: Final = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2})$")
# ``+30`` / ``-15`` relative form, seconds only. The leading sign is
# REQUIRED — a bare integer like ``30`` is treated as ``0:30`` absolute.
_SEEK_RELATIVE_RE: Final = re.compile(r"^([+-])(\d+)$")


def parse_seek_position(raw: str) -> tuple[bool, int] | None:
    """Parse a ``/seek`` argument into ``(is_relative, value_ms)``.

    Accepts:

    * Absolute ``M:S`` / ``M:SS`` / ``H:MM:SS`` (one or two colons; single-
      or double-digit seconds; minutes and seconds must each be ``< 60``).
    * Relative ``+N`` / ``-N`` (seconds, sign required).
    * Bare integer ``N`` — treated as absolute seconds (i.e. ``0:N``).

    Returns ``None`` for unparseable input. ``value_ms`` is the absolute
    target (when ``is_relative`` is False) or the signed delta in
    milliseconds (when ``is_relative`` is True). The caller clamps to
    ``[0, current.duration_ms]``.

    Pure: no side effects, no Lavalink calls, ready for unit tests.
    """
    raw = raw.strip()
    if not raw:
        return None
    rel = _SEEK_RELATIVE_RE.match(raw)
    if rel is not None:
        sign, digits = rel.groups()
        seconds = int(digits)
        return True, (-seconds if sign == "-" else seconds) * 1000
    abs_match = _SEEK_ABSOLUTE_RE.match(raw)
    if abs_match is not None:
        hours_str, minutes_str, seconds_str = abs_match.groups()
        hours = int(hours_str or 0)
        minutes = int(minutes_str)
        seconds = int(seconds_str)
        # Reject overflowed minute/second components — symmetric guards so
        # ``0:60:30`` is rejected just like ``1:60``.
        if seconds >= 60 or minutes >= 60:
            return None
        return False, ((hours * 3600) + (minutes * 60) + seconds) * 1000
    if raw.isdigit():
        return False, int(raw) * 1000
    return None


def build_queued_track_embed(
    track: TrackInfo,
    position: int,
    *,
    playing_now: bool,
    channel_id: int,
    requester_id: int,
    locale: str,
) -> hikari.Embed:
    """Build the embed for a single-track ``/play`` success (M1 §3).

    ``position`` is 1-indexed within the queue. ``playing_now`` swaps
    the trailing ``"position N in queue"`` for ``"playing now"`` when
    the queue was empty and the player was idle. ``channel_id`` is the
    voice channel ryzic joined; ``requester_id`` is the invoking user.
    Both render as Discord mentions in inline fields so users see a
    clickable channel/user pill — footer text would render them as raw
    ``<#…>`` / ``<@…>`` strings instead.
    """
    title = safe_truncate(escape_markdown(track.title), EMBED_DESCRIPTION_MAX // 2)
    uploader = safe_truncate(escape_markdown(track.uploader), EMBED_FOOTER_MAX // 4)
    description = safe_truncate(
        t("ux.queued.description", locale=locale, title=title, url=track.url),
        EMBED_DESCRIPTION_MAX,
    )
    duration = format_duration(track.duration_ms)
    if playing_now:
        footer = t(
            "ux.queued.footer.playing_now",
            locale=locale,
            uploader=uploader,
            duration=duration,
        )
    else:
        footer = t(
            "ux.queued.footer.in_queue",
            locale=locale,
            uploader=uploader,
            duration=duration,
            position=position,
        )
    embed = hikari.Embed(title=t("ux.queued.title", locale=locale), description=description)
    embed.add_field(
        name=t("ux.queued.field.channel.name", locale=locale),
        value=f"<#{channel_id}>",
        inline=True,
    )
    embed.add_field(
        name=t("ux.queued.field.requested_by.name", locale=locale),
        value=f"<@{requester_id}>",
        inline=True,
    )
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
    locale: str,
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
        t(
            "ux.queued_playlist.description",
            locale=locale,
            title=title,
            count=track_count,
            duration=duration,
        ),
        EMBED_DESCRIPTION_MAX,
    )
    if used_cache:
        embed_title = t("ux.queued_playlist.title.cached", locale=locale)
        when = (
            t("ux.queued_playlist.footer.cached_when_earlier", locale=locale)
            if fetched_at is None
            else _format_timestamp(fetched_at)
        )
        # Stale snapshots get a slightly louder mention so users notice
        # entries may have rotated; the fallback path itself is the same.
        suffix = (
            t("ux.queued_playlist.footer.cached_stale_suffix", locale=locale)
            if cache_is_stale
            else ""
        )
        footer = t(
            "ux.queued_playlist.footer.cached",
            locale=locale,
            when=when,
            suffix=suffix,
        )
    else:
        embed_title = t("ux.queued_playlist.title.live", locale=locale)
        footer = t(
            "ux.queued_playlist.footer.live",
            locale=locale,
            requester=requester,
        )
        if failed_count > 0:
            footer = t(
                "ux.queued_playlist.footer.failed_suffix",
                locale=locale,
                footer=footer,
                count=failed_count,
            )
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


def format_now_playing_line(
    track: TrackInfo,
    position_ms: int,
    *,
    paused: bool,
    locale: str,
) -> str:
    """One-line markdown summary of a now-playing track.

    Shape: ``[**title**](url) — M:SS / M:SS`` with ``(paused)`` appended
    when applicable. Title is markdown-escaped and length-capped so the
    line is safe to drop into any embed surface (`/np` description,
    ``/queue`` top hint, future controller embed).
    """
    title = safe_truncate(escape_markdown(track.title), EMBED_FIELD_VALUE_MAX // 2)
    position = format_duration(position_ms)
    duration = format_duration(track.duration_ms)
    key = "ux.np.line.paused" if paused else "ux.np.line.playing"
    return t(
        key,
        locale=locale,
        title=title,
        url=track.url,
        position=position,
        duration=duration,
    )


def build_simple_now_playing_embed(
    track: TrackInfo,
    position_ms: int,
    *,
    paused: bool,
    locale: str,
) -> hikari.Embed:
    """Build the ``/np`` embed for the currently-playing track.

    Coexists with :func:`build_now_playing_embed` (the persistent
    controller surface added in #90/#118): each command renders its own
    embed today. Unifying both around :func:`format_now_playing_line`
    so the controller and ``/np`` stay visually aligned is tracked as
    a follow-up; for now they diverge by design.
    """
    raw_line = format_now_playing_line(track, position_ms, paused=paused, locale=locale)
    line = safe_truncate(raw_line, EMBED_DESCRIPTION_MAX)
    uploader = safe_truncate(escape_markdown(track.uploader), EMBED_FOOTER_MAX // 4)
    embed = hikari.Embed(title=t("ux.np.title.playing", locale=locale), description=line)
    embed.set_footer(
        safe_truncate(
            t("ux.np.footer.by_uploader", locale=locale, uploader=uploader),
            EMBED_FOOTER_MAX,
        )
    )
    return embed


def build_queue_embed(
    *,
    now_playing: TrackInfo,
    now_playing_position_ms: int,
    paused: bool,
    queue: list[tuple[TrackInfo, int]],
    page: int = 1,
    total_pages: int = 1,
    locale: str,
) -> hikari.Embed:
    """Build the ``/queue`` embed (M1 §3, paging per issue #99).

    ``queue`` is the FULL list of ``(track_info, requester_id)`` tuples
    in queue order; this builder slices internally to render
    :data:`QUEUE_PAGE_SIZE` entries for the requested ``page``. The
    indexing in the description is global (page 2 starts at "11.", not
    "1."), so users can see where each visible track lives in the queue.
    Title gains a "(page X/Y)" suffix only when ``total_pages > 1`` —
    short queues that fit on a single page render unchanged. The
    currently-playing track is surfaced as a single ``Now: …`` hint at
    the top of the description — full now-playing detail belongs in
    ``/np``.
    """
    queue_count = len(queue)
    queue_total_ms = sum(info.duration_ms for info, _ in queue)
    title = t(
        "ux.queue.title",
        locale=locale,
        count=queue_count,
        duration=format_duration(queue_total_ms),
    )
    if total_pages > 1:
        title = t(
            "ux.queue.title_with_page",
            locale=locale,
            title=title,
            page=page,
            total_pages=total_pages,
        )

    now_line = format_now_playing_line(
        now_playing, now_playing_position_ms, paused=paused, locale=locale
    )
    queue_body = _build_queue_description(queue, page=page, locale=locale)
    description = (
        t("ux.queue.description.with_body", locale=locale, line=now_line, body=queue_body)
        if queue_body
        else t("ux.queue.description.now_only", locale=locale, line=now_line)
    )

    return hikari.Embed(
        title=title,
        description=safe_truncate(description, EMBED_DESCRIPTION_MAX),
    )


def _build_queue_description(
    queue: list[tuple[TrackInfo, int]],
    *,
    page: int,
    locale: str,
) -> str:
    """Format the queue-list portion of the ``/queue`` embed for ``page``.

    Returns the empty string when ``queue`` is empty so the caller can
    omit the blank-line separator after the ``Now: …`` hint. Indices are
    global (1-indexed against the full queue), so page 2 of a 25-track
    queue starts at "11." — gives users a stable mental model of where
    each visible track sits in the playback order.
    """
    if not queue:
        return ""
    start = (page - 1) * QUEUE_PAGE_SIZE
    end = start + QUEUE_PAGE_SIZE
    page_slice = queue[start:end]
    lines = [
        t(
            "ux.queue.entry",
            locale=locale,
            idx=idx,
            title=escape_markdown(info.title),
            url=info.url,
            duration=format_duration(info.duration_ms),
            requester_id=requester_id,
        )
        for idx, (info, requester_id) in enumerate(page_slice, start=start + 1)
    ]
    return safe_truncate("\n".join(lines), EMBED_DESCRIPTION_MAX)


def build_recent_embed(history: list[TrackInfo], *, locale: str) -> hikari.Embed:
    """Build the ``/recent`` embed (issue #96).

    ``history`` is newest-first. Caller guarantees non-empty (the
    command short-circuits on the empty case with a friendly ephemeral).
    Lines are 1-indexed so ``/replay <N>`` semantics match the displayed
    number directly.
    """
    preview = history[:_RECENT_PREVIEW_MAX]
    lines = [
        t(
            "ux.recent.entry",
            locale=locale,
            idx=idx,
            title=escape_markdown(info.title),
            url=info.url,
            duration=format_duration(info.duration_ms),
        )
        for idx, info in enumerate(preview, start=1)
    ]
    overflow = len(history) - len(preview)
    if overflow > 0:
        lines.append(t("ux.recent.overflow", locale=locale, count=overflow))
    description = safe_truncate("\n".join(lines), EMBED_DESCRIPTION_MAX)
    embed = hikari.Embed(
        title=t("ux.recent.title", locale=locale, count=len(history)),
        description=description,
    )
    embed.set_footer(t("ux.recent.footer", locale=locale))
    return embed


def build_now_playing_embed(
    track: TrackInfo,
    *,
    position_ms: int,
    paused: bool,
    queue_length: int,
    locale: str,
) -> hikari.Embed:
    """Build the persistent now-playing controller embed (issue #90).

    Distinct from :func:`build_queue_embed` to keep the controller
    surface visually narrow — it shows just current track + progress +
    queue depth, not the full queue listing. Title swaps to "Paused"
    when the player is paused so the embed matches the button state.
    """
    title = t(
        "ux.np.title.paused" if paused else "ux.np.title.playing",
        locale=locale,
    )
    safe_title = safe_truncate(escape_markdown(track.title), EMBED_DESCRIPTION_MAX // 2)
    description = safe_truncate(
        t("ux.np.description.title_link", locale=locale, title=safe_title, url=track.url),
        EMBED_DESCRIPTION_MAX,
    )
    progress = t(
        "ux.np.field.progress.value",
        locale=locale,
        position=format_duration(position_ms),
        duration=format_duration(track.duration_ms),
    )
    embed = hikari.Embed(title=title, description=description)
    embed.add_field(
        name=t("ux.np.field.progress.name", locale=locale),
        value=progress,
        inline=True,
    )
    embed.add_field(
        name=t("ux.np.field.up_next.name", locale=locale),
        value=t("ux.np.field.up_next.value", locale=locale, count=queue_length),
        inline=True,
    )
    uploader = safe_truncate(escape_markdown(track.uploader), EMBED_FOOTER_MAX // 4)
    embed.set_footer(
        safe_truncate(
            t("ux.np.footer.by_uploader", locale=locale, uploader=uploader),
            EMBED_FOOTER_MAX,
        )
    )
    return embed


def build_now_playing_idle_embed(*, locale: str) -> hikari.Embed:
    """Build the post-queue idle embed for the now-playing controller (issue #90).

    Used after ``QueueEndEvent`` and ``/leave`` so the controller stops
    advertising stale playback state. Buttons are rendered disabled by
    the caller to reinforce the inactive state.
    """
    return hikari.Embed(
        title=t("ux.np.title.idle", locale=locale),
        description=t("ux.np.idle.description", locale=locale),
    )
