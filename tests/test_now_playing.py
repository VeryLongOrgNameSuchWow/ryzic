"""Tests for ``ryzic.now_playing`` controller embed wiring."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

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
# #222 — disconnected render path (is_connected=False with current held)
# ---------------------------------------------------------------------------


async def test_refresh_renders_disabled_buttons_when_disconnected() -> None:
    """#215/#222: a held track with ``is_connected=False`` is the resync window.

    The command-side guard rejects /pause /resume /seek /skip with
    "Reconnecting to voice"; the controller must not advertise live buttons
    either. The held-track embed is reused (NOT the idle embed — ``current``
    is still held), and every button is disabled.
    """
    rest = _FakeRest(create_returns=9700)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="Held Track")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()

    # Lavalink reports disconnected while still holding the track.
    player.is_connected = False
    await now_playing.refresh(bot, 111)

    assert len(rest.edit_calls) == 1
    edit = rest.edit_calls[0]
    embed: hikari.Embed = edit["embed"]
    # Held-track embed, not the idle embed (whose "No tracks playing" copy
    # would be factually wrong — current is retained per #215).
    assert embed.title == t("ux.np.title.playing", locale="en_US")
    [row] = edit["components"]
    assert _disabled_in_row(row) == [True, True, True]


async def test_refresh_all_does_not_skip_disconnected_player() -> None:
    """#222: ``refresh_all`` deliberately re-renders disconnected players.

    The skip predicate does not skip a disconnected player (the paused
    term is narrowed to the connected case): re-rendering keeps the
    disabled-buttons visual during the resync window and is what
    auto-recovers to enabled buttons once ``is_connected`` returns True.
    Pins the decision so a future "optimization" adding ``is_connected``
    to the predicate doesn't silently drop the visual.
    """
    rest = _FakeRest(create_returns=9800)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)

    # Guild 111: PLAYING (control). Guild 222: disconnected but current held.
    for guild_id, connected in [(111, True), (222, False)]:
        player = ll.player_manager.create(guild_id=guild_id)
        player.is_connected = connected
        player.current = make_track_with_info()
        lavalink_glue.last_play_channel[guild_id] = guild_id * 10
        await now_playing.upsert_for_track_start(bot, guild_id)
    rest.edit_calls.clear()

    await now_playing.refresh_all(bot)

    edited_channels = {call["channel_id"] for call in rest.edit_calls}
    # Both re-rendered: the connected one advances progress, the
    # disconnected one re-renders disabled buttons.
    assert edited_channels == {111 * 10, 222 * 10}
    disconnected_edit = next(c for c in rest.edit_calls if c["channel_id"] == 222 * 10)
    [row] = disconnected_edit["components"]
    assert _disabled_in_row(row) == [True, True, True]


async def test_refresh_all_renders_disabled_for_disconnected_paused() -> None:
    """#222 regression: ``refresh_all`` must re-render a disconnected+PAUSED player.

    The paused-skip is narrowed to the connected case, so a paused player
    that enters the resync window (``is_connected=False``) is still
    re-rendered with disabled buttons. With the pre-fix predicate
    (``player.paused`` short-circuits before ``is_connected``) this player
    is skipped every cycle and its controller keeps an enabled Resume
    button — the exact #222 defect. Fails on the pre-fix predicate; passes
    after. (The 4014 teardown path bounds this window in practice; the
    non-4014 resync window leaves the record intact.)
    """
    rest = _FakeRest(create_returns=9810)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.paused = True
    player.current = make_track_with_info(title="Paused & Disconnected")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()

    player.is_connected = False  # disconnected while paused
    await now_playing.refresh_all(bot)

    assert len(rest.edit_calls) == 1
    edit = rest.edit_calls[0]
    [row] = edit["components"]
    assert _disabled_in_row(row) == [True, True, True]
    # Held-track embed keeps the Paused title; only the buttons change.
    assert edit["embed"].title == t("ux.np.title.paused", locale="en_US")


async def test_refresh_re_enables_buttons_after_reconnect() -> None:
    """#222: a playing player's buttons auto-recover on reconnect.

    ``refresh_all`` re-renders a disconnected player with disabled buttons,
    and once ``is_connected`` returns True the next cycle renders enabled
    buttons again — no separate recovery hook. Pins the auto-recovery
    transition so a future regression making the disabled state sticky
    (cached components, or skipping disconnected players) is caught.
    """
    rest = _FakeRest(create_returns=9820)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info(title="Recovering")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()

    # Disconnect: the controller flips to disabled buttons.
    player.is_connected = False
    await now_playing.refresh_all(bot)
    [row] = rest.edit_calls[-1]["components"]
    assert _disabled_in_row(row) == [True, True, True]

    # Reconnect: the next refresh_all cycle re-enables the buttons.
    rest.edit_calls.clear()
    player.is_connected = True
    await now_playing.refresh_all(bot)
    assert len(rest.edit_calls) == 1
    [row] = rest.edit_calls[-1]["components"]
    assert _disabled_in_row(row) == [False, False, False]


async def test_refresh_all_leaves_paused_controller_disabled_until_resume_after_reconnect() -> None:
    """Pins the documented paused-reconnect residual (#222 follow-up review).

    A paused player that disconnects is re-rendered with disabled buttons
    (the fix). Once it reconnects while STILL paused, ``refresh_all`` skips
    it again (paused+connected => byte-identical), so the controller stays
    on the disabled render until the next user-initiated ``/resume`` after
    reconnection. This is a bounded transient and strictly safer than the
    #222 defect (disabled-when-should-be-enabled, vs the original
    enabled-when-should-be-disabled). A future fix that re-enables the
    paused row on reconnect should update this test.
    """
    rest = _FakeRest(create_returns=9821)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.paused = True
    player.current = make_track_with_info(title="Paused Reconnect")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()

    # Disconnect while paused: the controller flips to disabled buttons.
    player.is_connected = False
    await now_playing.refresh_all(bot)
    [row] = rest.edit_calls[-1]["components"]
    assert _disabled_in_row(row) == [True, True, True]

    # Reconnect while still paused: refresh_all skips it (paused+connected),
    # so no edit lands and the controller stays on the disabled render.
    rest.edit_calls.clear()
    player.is_connected = True
    await now_playing.refresh_all(bot)
    assert rest.edit_calls == []


async def test_upsert_renders_disabled_buttons_when_disconnected() -> None:
    """#222: the create path also renders disabled buttons when disconnected.

    ``upsert_for_track_start`` -> ``_render_for_player`` shares the
    disconnected branch, so a track that starts during the resync window
    renders with disabled buttons (not the enabled row). TrackStart
    normally implies a voice connection, but the branch is shared and the
    case is cheap to pin.
    """
    rest = _FakeRest(create_returns=9830)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = False
    player.current = make_track_with_info(title="Started While Disconnected")
    lavalink_glue.last_play_channel[111] = 555

    await now_playing.upsert_for_track_start(bot, 111)

    assert len(rest.create_calls) == 1
    call = rest.create_calls[0]
    [row] = call["components"]
    assert _disabled_in_row(row) == [True, True, True]
    embed: hikari.Embed = call["embed"]
    assert embed.title == t("ux.np.title.playing", locale="en_US")


async def test_render_disconnected_takes_precedence_over_paused() -> None:
    """#222: a disconnected+paused player shows disabled buttons, not Resume.

    Disconnected must win over paused in the component selection — a
    disconnected player must not advertise a clickable Resume even when the
    held track is also paused.
    """
    rest = _FakeRest(create_returns=9900)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.paused = True
    player.current = make_track_with_info(title="Paused & Disconnected")
    lavalink_glue.last_play_channel[111] = 555
    await now_playing.upsert_for_track_start(bot, 111)
    rest.edit_calls.clear()

    player.is_connected = False  # disconnected while paused
    await now_playing.refresh(bot, 111)

    assert len(rest.edit_calls) == 1
    edit = rest.edit_calls[0]
    [row] = edit["components"]
    # All disabled — no enabled Resume button from the paused row.
    assert _disabled_in_row(row) == [True, True, True]
    # The held-track embed keeps the Paused title (paused=player.paused is
    # passed through); only the buttons change. Pins the body so a future
    # "Reconnecting" title variant doesn't silently change visible copy.
    assert edit["embed"].title == t("ux.np.title.paused", locale="en_US")


# ---------------------------------------------------------------------------
# refresh_all — per-guild skip filter + exception isolation (#200)
# ---------------------------------------------------------------------------


async def test_refresh_all_skips_paused_and_idle_players() -> None:
    """``refresh_all`` only edits controllers whose progress is moving.

    A PLAYING player triggers an edit; a PAUSED player and an IDLE player
    (``current is None``) are skipped so the loop doesn't issue
    byte-identical edits every cycle (edit spam / REST quota burns). A
    regression dropping the paused-skip guard must fail here.
    """
    rest = _FakeRest(create_returns=10_000)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)

    # Three guilds, each with an existing controller (so refresh() has a
    # record to edit). Seed distinct player states.
    for guild_id, paused, has_current in [
        (111, False, True),  # PLAYING -> should be edited
        (222, True, True),  # PAUSED -> skipped
        (333, False, False),  # IDLE (current is None) -> skipped
    ]:
        player = ll.player_manager.create(guild_id=guild_id)
        player.is_connected = True
        player.paused = paused
        player.current = make_track_with_info() if has_current else None
        lavalink_glue.last_play_channel[guild_id] = guild_id * 10
        await now_playing.upsert_for_track_start(bot, guild_id)
    rest.edit_calls.clear()

    await now_playing.refresh_all(bot)

    edited_guilds = {call["channel_id"] for call in rest.edit_calls}
    assert edited_guilds == {111 * 10}


async def test_refresh_all_skips_guilds_with_no_player() -> None:
    """A guild whose player disappeared (None) is skipped, not edited."""
    rest = _FakeRest(create_returns=11_000)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)

    # Guild 111: PLAYING -> edited. Guild 222: controller record exists
    # but lavalink_glue.get_player returns None -> skipped.
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = make_track_with_info()
    lavalink_glue.last_play_channel[111] = 1110
    await now_playing.upsert_for_track_start(bot, 111)

    # Plant a controller record for 222 without creating a player.
    now_playing._controllers[222] = (2220, 9999)
    rest.edit_calls.clear()

    await now_playing.refresh_all(bot)

    edited_channels = {call["channel_id"] for call in rest.edit_calls}
    assert edited_channels == {1110}


async def test_refresh_all_isolates_per_guild_exceptions() -> None:
    """One guild's raising refresh does NOT abort the loop for other guilds.

    The loop catches per-guild exceptions so a single REST failure (or an
    unexpected bug for one guild) doesn't starve the rest of the
    controllers' progress-bar advances.
    """
    # Two PLAYING guilds. Guild 111's edit raises a HikariError (logged +
    # swallowed inside _post_or_edit); guild 222's edit must still run.
    # Use NotFoundError on 111 to force the create fallback path, then
    # make the create also fail so the refresh for 111 raises within
    # _post_or_edit's HikariError handling — actually _post_or_edit logs
    # and returns on HikariError, so the per-guild try/except in
    # refresh_all is exercised by patching refresh to raise for 111 only.
    rest = _FakeRest(create_returns=12_000)
    bot = _bot_with_rest(rest)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)

    for guild_id in (111, 222):
        player = ll.player_manager.create(guild_id=guild_id)
        player.is_connected = True
        player.current = make_track_with_info()
        lavalink_glue.last_play_channel[guild_id] = guild_id * 10
        await now_playing.upsert_for_track_start(bot, guild_id)
    rest.edit_calls.clear()

    real_refresh = now_playing.refresh

    async def flaky_refresh(b, gid):
        if gid == 111:
            raise RuntimeError("simulated per-guild failure")
        await real_refresh(b, gid)

    with patch.object(now_playing, "refresh", side_effect=flaky_refresh):
        await now_playing.refresh_all(bot)

    # Guild 222 still got its edit despite guild 111 raising.
    edited_channels = {call["channel_id"] for call in rest.edit_calls}
    assert 222 * 10 in edited_channels
    assert 111 * 10 not in edited_channels


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


def _disabled_in_row(row: hikari.api.MessageActionRowBuilder) -> list[bool]:
    """Extract per-button ``disabled`` flags from an action-row builder."""
    payload, _ = row.build()
    components = payload["components"]
    return [bool(comp["disabled"]) for comp in components]


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
