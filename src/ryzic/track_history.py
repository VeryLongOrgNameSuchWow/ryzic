"""Per-guild ring buffer of recently-played tracks (issue #96).

Populated by ``lavalink_glue.EventHandler.on_track_end`` only on the
:class:`lavalink.server.EndReason` values that represent a track the
user actually heard (``FINISHED`` for natural end, ``REPLACED`` for
``/skip``-style mid-track advance). ``LOAD_FAILED`` / ``STOPPED`` /
``CLEANUP`` are excluded — failures were never heard, ``/leave``
explicitly winds down the session, and Lavalink cleanup events don't
correspond to user-facing playback.

State is in-memory only — by design (issue #96 hard line). Cross-session
persistence is a separate, out-of-scope feature with its own design
question. The bot's restart drops history, which matches every other
"this is what just played" surface in Discord.

Module-level state (mirrors ``lavalink_glue``'s singletons): a single
process owns one history per guild. Tests reset via
:func:`_reset_state_for_test`.
"""

from __future__ import annotations

from collections import deque

from .ytdlp import TrackInfo

# Bounded buffer. 25 lines comfortably fits one Discord embed
# description (~4096 chars) even with long titles. Issue #96 explicitly
# called the cap as "20-50"; 25 is the midpoint that matches the
# audit's competitor-survey median.
MAX_HISTORY_SIZE = 25


_history: dict[int, deque[TrackInfo]] = {}


def record(guild_id: int, track: TrackInfo) -> None:
    """Push ``track`` onto the front of ``guild_id``'s history ring.

    Newest entry is always at index 0; the deque's left-side push +
    ``maxlen`` evicts the oldest from the right when the cap is reached.
    Idempotent against the same track replaying back-to-back: we still
    record it (history is per-event, not per-unique-track) so a user who
    just replayed sees their action reflected.
    """
    ring = _history.setdefault(guild_id, deque(maxlen=MAX_HISTORY_SIZE))
    ring.appendleft(track)


def get(guild_id: int) -> list[TrackInfo]:
    """Return ``guild_id``'s history newest-first, or ``[]`` if empty."""
    ring = _history.get(guild_id)
    if ring is None:
        return []
    return list(ring)


def _reset_state_for_test() -> None:
    """Test-only: clear all per-guild history state."""
    _history.clear()
