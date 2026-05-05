"""Tests for ``ryzic.track_history``."""

from __future__ import annotations

import pytest

from ryzic import track_history
from tests._command_helpers import make_track_info


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    track_history._reset_state_for_test()


def test_empty_guild_returns_empty_list() -> None:
    assert track_history.get(111) == []


def test_record_pushes_newest_first() -> None:
    a = make_track_info(video_id="aaaaaaaaaaa", title="A")
    b = make_track_info(video_id="bbbbbbbbbbb", title="B")
    track_history.record(111, a)
    track_history.record(111, b)

    history = track_history.get(111)
    assert [t.title for t in history] == ["B", "A"]


def test_record_evicts_oldest_at_cap() -> None:
    """Cap enforced: oldest entry falls off the right end of the deque."""
    for i in range(track_history.MAX_HISTORY_SIZE + 5):
        track_history.record(111, make_track_info(video_id=f"vid{i:08d}", title=f"T{i}"))

    history = track_history.get(111)
    assert len(history) == track_history.MAX_HISTORY_SIZE
    # Newest is the last we recorded; oldest visible is offset by 5.
    assert history[0].title == f"T{track_history.MAX_HISTORY_SIZE + 4}"
    assert history[-1].title == "T5"


def test_record_repeated_track_keeps_each_event() -> None:
    """History is per-event; recording the same track twice yields two entries."""
    track = make_track_info(title="Solo")
    track_history.record(111, track)
    track_history.record(111, track)

    assert [t.title for t in track_history.get(111)] == ["Solo", "Solo"]


def test_per_guild_isolation() -> None:
    track_history.record(111, make_track_info(video_id="aaaaaaaaaaa", title="A"))
    track_history.record(222, make_track_info(video_id="bbbbbbbbbbb", title="B"))

    assert [t.title for t in track_history.get(111)] == ["A"]
    assert [t.title for t in track_history.get(222)] == ["B"]


def test_get_returns_snapshot_not_internal_deque() -> None:
    """``get`` returns a list copy; mutating it must not affect the ring."""
    track_history.record(111, make_track_info(title="A"))
    snap = track_history.get(111)
    snap.clear()

    assert len(track_history.get(111)) == 1


def test_reset_state_clears_all_guilds() -> None:
    track_history.record(111, make_track_info(title="A"))
    track_history.record(222, make_track_info(title="B"))

    track_history._reset_state_for_test()

    assert track_history.get(111) == []
    assert track_history.get(222) == []
