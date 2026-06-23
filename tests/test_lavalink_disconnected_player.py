"""Characterization tests pinning lavalink 5.11.0's disconnected-player behaviour.

These tests exercise the REAL :class:`lavalink.DefaultPlayer` against a stub
node whose ``update_player`` is an :class:`~unittest.mock.AsyncMock`. They pin
the external-library behaviour that issue #215's reject-up-front stance relies
on: ``set_pause`` / ``seek`` / ``stop`` / ``skip`` do NOT consult
``is_connected`` — they always send an HTTP PATCH via ``node.update_player``
regardless of whether Lavalink considers the player connected.

A future lavalink bump that adds an ``is_connected`` short-circuit (or stops
PATCHing on a disconnected player) should fail here, which is the signal to
re-evaluate ryzic's #215 guard policy (reject-with-reconnecting vs. proceed).

Version-fragile by design — see ``docs/plans`` / #215 for the rationale.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import lavalink
from lavalink.server import AudioTrack


def _make_player() -> tuple[lavalink.DefaultPlayer, Any]:
    """Build a real ``DefaultPlayer`` with a stub node + client.

    Returns ``(player, update_player_mock)`` so tests can assert on the
    mock without ty balking at the real ``Node.update_player`` overload
    signature (which has no ``assert_awaited_once``).

    ``is_connected`` is a read-only property (``self.channel_id is not None``)
    on lavalink 5.11.0, so we leave ``channel_id`` at its constructor default
    of ``None`` to obtain ``is_connected=False`` — do NOT try to set the
    property directly.
    """
    update_player = AsyncMock(return_value={})
    node = MagicMock()
    node.update_player = update_player
    node.manager = MagicMock()
    node.manager.client = MagicMock()
    node.manager.client._dispatch_event = MagicMock()

    player = lavalink.DefaultPlayer(guild_id=111, node=node)
    # ``channel_id`` stays ``None`` ⇒ ``is_connected`` is False.
    return player, update_player


def _make_track() -> AudioTrack:
    """Build a real :class:`AudioTrack` with a base64 ``encoded`` string.

    ``play_track`` reads ``track.track`` (the base64 payload, derived from
    ``data['encoded']``); without it the play path raises. We only need it
    for the ``skip`` → ``play`` path when the queue is non-empty; the
    empty-queue path goes via ``stop`` and does not consult ``track``.
    """
    raw: Any = {
        "encoded": "QAAAAAAAAA",
        "info": {
            "title": "x",
            "author": "y",
            "length": 213_000,
            "identifier": "id",
            "uri": "https://example/x",
            "isStream": False,
            "isSeekable": True,
        },
    }
    return AudioTrack(raw, requester=0)


async def test_set_pause_sends_patch_regardless_of_is_connected() -> None:
    player, update_player = _make_player()
    player.current = _make_track()
    assert player.is_connected is False  # sanity: channel_id is None

    await player.set_pause(True)

    update_player.assert_awaited_once()
    _, kwargs = update_player.call_args
    assert kwargs["guild_id"] == "111"
    assert kwargs["paused"] is True


async def test_seek_sends_patch_regardless_of_is_connected() -> None:
    player, update_player = _make_player()
    player.current = _make_track()

    await player.seek(120_000)

    update_player.assert_awaited_once()
    _, kwargs = update_player.call_args
    assert kwargs["position"] == 120_000


async def test_stop_sends_patch_regardless_of_is_connected() -> None:
    player, update_player = _make_player()
    player.current = _make_track()

    await player.stop()

    update_player.assert_awaited_once()
    _, kwargs = update_player.call_args
    assert kwargs["encoded_track"] is None
    assert player.current is None


async def test_skip_delegates_to_play_and_patches() -> None:
    """``skip`` delegates to ``play``; with an empty queue ``play`` calls ``stop``,
    which PATCHes ``encoded_track=None``. Either way ``update_player`` fires
    even though ``is_connected`` is False — the client does not short-circuit.
    """
    player, update_player = _make_player()
    player.current = _make_track()
    assert player.queue == []

    await player.skip()

    update_player.assert_awaited()
    _, kwargs = update_player.call_args
    assert kwargs["encoded_track"] is None


async def test_is_connected_is_read_only_property_derived_from_channel_id() -> None:
    """Pin the property definition: setting ``channel_id`` flips ``is_connected``.

    If a future lavalink version makes ``is_connected`` a settable attribute or
    derives it differently, this test fails and the #215 helper's
    ``not player.is_connected`` branch needs re-evaluation.
    """
    player, _ = _make_player()
    assert player.is_connected is False
    player.channel_id = 999
    assert player.is_connected is True
