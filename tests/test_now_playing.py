"""Tests for ``ryzic.now_playing`` controller embed wiring."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import hikari
import lavalink
import pytest

from ryzic import lavalink_glue, now_playing
from ryzic.i18n import t
from tests._command_helpers import (
    FakeAudioTrack,
    FakeLavalinkClient,
    install_lavalink_client,
    make_track_with_info,
)


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _FakeRest:
    """Captures REST calls so we can assert the controller's edit path."""

    def __init__(
        self,
        *,
        create_returns: int = 9000,
        edit_error: Exception | None = None,
        create_error: Exception | None = None,
    ) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self._next_id = create_returns
        self._edit_error = edit_error
        self._create_error = create_error

    async def create_message(self, channel_id: int, **kwargs: Any) -> _FakeMessage:
        self.create_calls.append({"channel_id": channel_id, **kwargs})
        if self._create_error is not None:
            raise self._create_error
        message_id = self._next_id
        self._next_id += 1
        return _FakeMessage(message_id)

    async def edit_message(self, channel_id: int, message_id: int, **kwargs: Any) -> _FakeMessage:
        self.edit_calls.append({"channel_id": channel_id, "message_id": message_id, **kwargs})
        if self._edit_error is not None:
            raise self._edit_error
        return _FakeMessage(message_id)


def _bot_with_rest(rest: _FakeRest) -> hikari.GatewayBot:
    bot = MagicMock(spec=hikari.GatewayBot)
    bot.rest = rest
    return cast(hikari.GatewayBot, bot)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    now_playing._reset_state_for_test()
    install_lavalink_client(None)


# ---------------------------------------------------------------------------
# upsert_for_track_start
# ---------------------------------------------------------------------------


async def test_upsert_no_last_play_channel_no_op() -> None:
    """No record → no controller; calling refresh in unrelated guilds is a no-op."""
    rest = _FakeRest()
    bot = _bot_with_rest(rest)
    install_lavalink_client(FakeLavalinkClient())

    await now_playing.upsert_for_track_start(bot, 111)

    assert rest.create_calls == []
    assert rest.edit_calls == []


async def test_upsert_creates_controller_when_track_playing() -> None:
    rest = _FakeRest(create_returns=9001)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="Now Playing")
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.upsert_for_track_start(bot, 111)

    assert len(rest.create_calls) == 1
    call = rest.create_calls[0]
    assert call["channel_id"] == 555
    assert "embed" in call and "components" in call
    assert now_playing.is_known_message(111, 9001)


async def test_upsert_idle_when_no_player() -> None:
    """No player but ``last_play_channel`` set → render the idle embed."""
    rest = _FakeRest()
    bot = _bot_with_rest(rest)
    install_lavalink_client(FakeLavalinkClient())  # no player created
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.upsert_for_track_start(bot, 111)

    assert len(rest.create_calls) == 1
    embed: hikari.Embed = rest.create_calls[0]["embed"]
    assert embed.title == t("ux.np.title.idle", locale="en_US")


async def test_upsert_idle_when_track_lacks_metadata() -> None:
    """Bare AudioTrack → idle (don't render half-populated)."""
    rest = _FakeRest()
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(title="bare")
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.upsert_for_track_start(bot, 111)

    assert len(rest.create_calls) == 1
    embed: hikari.Embed = rest.create_calls[0]["embed"]
    assert embed.title == t("ux.np.title.idle", locale="en_US")


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_no_op_without_existing_controller() -> None:
    """``refresh`` does NOT create a new controller; only TrackStart can."""
    rest = _FakeRest()
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.refresh(bot, 111)

    assert rest.create_calls == []
    assert rest.edit_calls == []


async def test_refresh_edits_existing_controller_to_paused() -> None:
    rest = _FakeRest(create_returns=9100)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="My Track")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()  # ignore the post call's history

    player.paused = True
    await now_playing.refresh(bot, 111)

    assert len(rest.edit_calls) == 1
    edit = rest.edit_calls[0]
    assert edit["channel_id"] == 555
    assert edit["message_id"] == 9100
    embed: hikari.Embed = edit["embed"]
    assert embed.title == t("ux.np.title.paused", locale="en_US")


async def test_refresh_renders_idle_when_player_clears() -> None:
    rest = _FakeRest(create_returns=9200)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)

    # Queue end ⇒ current cleared
    player.current = None
    await now_playing.refresh(bot, 111)

    embed: hikari.Embed = rest.edit_calls[-1]["embed"]
    assert embed.title == t("ux.np.title.idle", locale="en_US")


async def test_refresh_recovers_from_deleted_message_by_reposting() -> None:
    rest = _FakeRest(
        create_returns=9300,
        edit_error=hikari.NotFoundError(url="x", headers={}, raw_body=b""),  # type: ignore[arg-type]
    )
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()
    rest.create_calls.clear()

    # Edit fails with NotFoundError → fall through to a fresh post.
    rest._next_id = 9301
    await now_playing.refresh(bot, 111)

    assert len(rest.create_calls) == 1
    assert now_playing.is_known_message(111, 9301)


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


async def test_teardown_no_op_without_controller() -> None:
    rest = _FakeRest()
    bot = _bot_with_rest(rest)

    await now_playing.teardown(bot, 111)

    assert rest.edit_calls == []
    assert rest.create_calls == []


async def test_teardown_finalizes_controller_to_idle_and_clears_record() -> None:
    rest = _FakeRest(create_returns=9400)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)

    await now_playing.teardown(bot, 111)

    assert not now_playing.is_known_message(111, 9400)
    edit = rest.edit_calls[-1]
    embed: hikari.Embed = edit["embed"]
    assert embed.title == t("ux.np.title.idle", locale="en_US")


async def test_teardown_swallows_not_found_when_message_already_gone() -> None:
    rest = _FakeRest(
        create_returns=9500,
        edit_error=hikari.NotFoundError(url="x", headers={}, raw_body=b""),  # type: ignore[arg-type]
    )
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)

    # Should not raise even though edit_message will 404.
    await now_playing.teardown(bot, 111)

    assert not now_playing.is_known_message(111, 9500)


# ---------------------------------------------------------------------------
# is_known_message
# ---------------------------------------------------------------------------


def test_is_known_message_true_for_active_controller() -> None:
    now_playing._controllers[111] = (555, 12345)
    assert now_playing.is_known_message(111, 12345)


def test_is_known_message_false_for_unknown_guild() -> None:
    assert not now_playing.is_known_message(111, 12345)


def test_is_known_message_false_for_different_message() -> None:
    now_playing._controllers[111] = (555, 12345)
    assert not now_playing.is_known_message(111, 99999)


# ---------------------------------------------------------------------------
# Embed rate-limit consideration: edit-once per state change
# ---------------------------------------------------------------------------


async def test_track_start_creates_then_subsequent_changes_edit_in_place() -> None:
    """A play→pause→resume sequence should be 1 create + 2 edits, not 3 creates."""
    rest = _FakeRest(create_returns=9600)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.upsert_for_track_start(bot, 111)
    player.paused = True
    await now_playing.refresh(bot, 111)
    player.paused = False
    await now_playing.refresh(bot, 111)

    assert len(rest.create_calls) == 1
    assert len(rest.edit_calls) == 2


async def test_lavalink_default_player_typing_uses_player_manager_get() -> None:
    """Smoke: ensure FakeLavalinkClient's player_manager protocol matches usage."""
    ll = FakeLavalinkClient()
    cast_player = ll.player_manager.get(111)
    assert cast_player is None  # nothing created yet
    cast_lavalink = cast(lavalink.Client, ll)
    assert cast_lavalink.player_manager.get(111) is None


# ---------------------------------------------------------------------------
# Button labels — #147 fold: Stop renamed to Leave
# ---------------------------------------------------------------------------


def _labels_in_row(row: hikari.api.MessageActionRowBuilder) -> list[str]:
    """Extract button labels from an action-row builder via the build payload."""
    payload, _ = row.build()
    components = payload["components"]
    return [comp["label"] for comp in components]


def test_active_row_renders_expected_button_labels() -> None:
    """#174 fold: the stop-styled button surfaces both effects via 'Stop & leave'."""
    [row] = now_playing._build_components()
    assert _labels_in_row(row) == [
        t("controller.button.pause", locale="en_US"),
        t("controller.button.skip", locale="en_US"),
        t("controller.button.leave", locale="en_US"),
    ]
    assert "Stop & leave" in _labels_in_row(row)


def test_paused_row_renders_expected_button_labels() -> None:
    [row] = now_playing._build_components_paused()
    assert _labels_in_row(row) == [
        t("controller.button.resume", locale="en_US"),
        t("controller.button.skip", locale="en_US"),
        t("controller.button.leave", locale="en_US"),
    ]


def test_idle_row_renders_expected_button_labels() -> None:
    [row] = now_playing._build_idle_components()
    assert _labels_in_row(row) == [
        t("controller.button.pause", locale="en_US"),
        t("controller.button.skip", locale="en_US"),
        t("controller.button.leave", locale="en_US"),
    ]
