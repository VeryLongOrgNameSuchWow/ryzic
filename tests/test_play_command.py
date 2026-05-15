"""Tests for ``ryzic.commands.play``.

The /play handler glues together yt-dlp, the audio cache, lavalink,
and Discord. We mock each collaborator at its module boundary so each
test exercises a single decision branch:

* URL validation, playlist URL detection, friendly error mapping.
* Voice precondition checks (DM, no voice, stage channel, bot in
  another channel).
* Audio service availability (cache singleton, lavalink client +
  available nodes).
* Queue-cap enforcement.
* Voice handshake timeout.
* Per-track load failures + cache release contract.
* Embed plumbing for both the single-track and playlist branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import hikari
import lavalink
import lightbulb
import pytest

from ryzic import audio_cache, lavalink_glue, ytdlp
from ryzic.commands import play as play_module
from ryzic.errors import FetchFailed
from ryzic.i18n import t
from ryzic.ytdlp import PlaylistInfo, TrackInfo

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeUser:
    id: int
    username: str = "alice"


@dataclass
class _FakeVoiceState:
    channel_id: int | None


@dataclass
class _FakeChannel:
    type: hikari.ChannelType


class _FakeCache:
    def __init__(
        self,
        states: dict[tuple[int, int], _FakeVoiceState | None] | None = None,
        channels: dict[int, _FakeChannel] | None = None,
    ) -> None:
        self._states = states or {}
        self._channels = channels or {}

    def get_voice_state(self, guild_id: int, user_id: int) -> _FakeVoiceState | None:
        return self._states.get((guild_id, user_id))

    def get_guild_channel(self, channel_id: int) -> _FakeChannel | None:
        return self._channels.get(channel_id)


class _FakeBot:
    """``hikari.GatewayBot`` slim stand-in covering exactly the surface /play uses."""

    def __init__(
        self,
        bot_user_id: int = 10,
        states: dict[tuple[int, int], _FakeVoiceState | None] | None = None,
        channels: dict[int, _FakeChannel] | None = None,
    ) -> None:
        self._me = _FakeUser(bot_user_id)
        self.cache = _FakeCache(states, channels)
        self.update_voice_state_calls: list[tuple[int, int | None]] = []

    def get_me(self) -> _FakeUser:
        return self._me

    async def update_voice_state(
        self,
        guild_id: int,
        channel_id: int | None,
        *,
        self_deaf: bool = False,
    ) -> None:
        self.update_voice_state_calls.append((guild_id, channel_id))


class _FakeLightbulbClient:
    def __init__(self, app: _FakeBot) -> None:
        self.app = app


class _FakeInteraction:
    """Stand-in for ``ctx.interaction`` so ``locale_for_*`` resolvers run.

    Neither ``locale`` nor ``guild_locale`` is set — the resolvers fall
    back to ``en_US``, matching the byte-identical-English assertions.
    """


@dataclass
class _FakeCommandData:
    name: str = "play"


class _FakeContext:
    def __init__(
        self,
        bot: _FakeBot,
        guild_id: int | None = 111,
        user_id: int = 222,
        channel_id: int = 555,
        command_name: str = "play",
    ) -> None:
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.channel_id = channel_id
        self.client = _FakeLightbulbClient(bot)
        self.interaction = _FakeInteraction()
        self.command_data = _FakeCommandData(name=command_name)
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.deferred = False

    async def defer(self) -> None:
        self.deferred = True

    async def respond(self, content: Any = None, **kwargs: Any) -> None:
        self.responses.append((content, kwargs))


@dataclass
class _FakeNode:
    available: bool = True
    name: str = "test"
    region: str = "us"
    get_tracks_results: list[Any] = field(default_factory=list)
    get_tracks_calls: list[str] = field(default_factory=list)

    async def get_tracks(self, query: str) -> Any:
        self.get_tracks_calls.append(query)
        if self.get_tracks_results:
            result = self.get_tracks_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return _FakeLoadResult.empty()


class _FakeNodeManager:
    def __init__(self, nodes: list[_FakeNode]) -> None:
        self.nodes = nodes

    @property
    def available_nodes(self) -> list[_FakeNode]:
        return [n for n in self.nodes if n.available]


@dataclass
class _FakeAudioTrack:
    """Slim AudioTrack stand-in — covers the duck shape ``player.add`` reads."""

    title: str = "Some Song"
    author: str = "Some Artist"
    identifier: str = "abc123"
    duration: int = 213_000
    uri: str = "https://example.com/x"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeLoadResult:
    load_type: lavalink.server.LoadType
    tracks: list[Any]
    error: Any | None = None

    @classmethod
    def empty(cls) -> _FakeLoadResult:
        return cls(load_type=lavalink.server.LoadType.EMPTY, tracks=[])


class _FakePlayer:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.queue: list[Any] = []
        self.is_playing: bool = False
        self.paused: bool = False
        self.play_called: int = 0
        self.added: list[tuple[Any, int]] = []

    def add(self, track: Any, requester: int = 0, index: Any = None) -> None:
        self.queue.append(track)
        self.added.append((track, requester))

    async def play(self) -> None:
        self.play_called += 1
        # Mimic lavalink: pop one off the queue + start it.
        if self.queue:
            self.queue.pop(0)
        self.is_playing = True


class _FakePlayerManager:
    def __init__(self) -> None:
        self.players: dict[int, _FakePlayer] = {}

    def create(self, guild_id: int, **kwargs: Any) -> _FakePlayer:
        if guild_id not in self.players:
            self.players[guild_id] = _FakePlayer(guild_id)
        return self.players[guild_id]

    def get(self, guild_id: int) -> _FakePlayer | None:
        return self.players.get(guild_id)


class _FakeLavalinkClient:
    def __init__(self, nodes: list[_FakeNode] | None = None) -> None:
        self.node_manager = _FakeNodeManager(nodes if nodes is not None else [_FakeNode()])
        self.player_manager = _FakePlayerManager()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Wipe the audio cache + lavalink singletons between tests."""
    audio_cache.set_audio_cache(None)
    lavalink_glue._reset_state_for_test()
    lavalink_glue._set_lavalink_client_for_test(None)
    # Per-guild first-play-tip tracking lives at module scope; reset so
    # tests get a fresh "first play" world.
    play_module._reset_state_for_test()


@pytest.fixture
async def cache(tmp_path: Path) -> Any:
    c = audio_cache.AudioCache(tmp_path, max_bytes=10_000_000)
    await c.open()
    audio_cache.set_audio_cache(c)
    try:
        yield c
    finally:
        audio_cache.set_audio_cache(None)
        await c.close()


def _bot_in_voice_with(
    user_channel_id: int,
    bot_channel_id: int | None = None,
    user_id: int = 222,
    bot_user_id: int = 10,
    channel_type: hikari.ChannelType = hikari.ChannelType.GUILD_VOICE,
) -> _FakeBot:
    states: dict[tuple[int, int], _FakeVoiceState | None] = {
        (111, user_id): _FakeVoiceState(channel_id=user_channel_id),
    }
    if bot_channel_id is not None:
        states[(111, bot_user_id)] = _FakeVoiceState(channel_id=bot_channel_id)
    channels = {user_channel_id: _FakeChannel(type=channel_type)}
    return _FakeBot(bot_user_id=bot_user_id, states=states, channels=channels)


def _track(video_id: str = "dQw4w9WgXcQ") -> TrackInfo:
    return TrackInfo(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Test Track",
        uploader="Tester",
        duration_ms=180_000,
    )


def _ll_with_one_node() -> tuple[_FakeLavalinkClient, _FakeNode]:
    node = _FakeNode()
    return _FakeLavalinkClient(nodes=[node]), node


# ---------------------------------------------------------------------------
# URL validation + DM rejection
# ---------------------------------------------------------------------------


async def test_dm_invocation_returns_friendly_error() -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot, guild_id=None)
    await play_module._handle_play(cast(lightbulb.Context, ctx), "https://www.youtube.com/")
    assert ctx.responses[0][0] == t("voice.error.run_in_server", locale="en_US", command="play")
    assert ctx.responses[0][0] == "Run /play in a server."
    assert ctx.responses[0][1].get("ephemeral") is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/watch?v=abc",
        "http://www.youtube.com/watch?v=abc",  # not https
        "ftp://youtube.com/x",
        "not-a-url",
    ],
)
async def test_unsupported_url_rejected_before_io(url: str) -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot)
    await play_module._handle_play(cast(lightbulb.Context, ctx), url)
    assert ctx.responses[0][0] == t("ytdlp.error.unsupported_url", locale="en_US")
    assert (
        ctx.responses[0][0]
        == "Only YouTube URLs are supported. Paste a link like https://youtu.be/dQw4w9WgXcQ."
    )


# ---------------------------------------------------------------------------
# Audio service availability
# ---------------------------------------------------------------------------


async def test_missing_audio_cache_maps_to_audio_service_down() -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot)
    # No audio_cache singleton installed.
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, _FakeLavalinkClient()))
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t("play.error.audio_service_down", locale="en_US")
    assert ctx.responses[0][0] == "Audio service is down. Try again in a minute."


async def test_missing_lavalink_client_maps_to_audio_service_down(cache: Any) -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot)
    # No lavalink client installed.
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t("play.error.audio_service_down", locale="en_US")
    assert ctx.responses[0][0] == "Audio service is down. Try again in a minute."


async def test_no_available_nodes_maps_to_audio_service_down(cache: Any) -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot)
    ll = _FakeLavalinkClient(nodes=[_FakeNode(available=False)])
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t("play.error.audio_service_down", locale="en_US")
    assert ctx.responses[0][0] == "Audio service is down. Try again in a minute."


# ---------------------------------------------------------------------------
# Voice precondition checks
# ---------------------------------------------------------------------------


async def test_user_not_in_voice_returns_friendly_error(cache: Any) -> None:
    bot = _FakeBot()
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t("play.error.join_voice_first", locale="en_US")
    assert ctx.responses[0][0] == "Join a voice channel first."


async def test_user_in_stage_channel_rejected(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999, channel_type=hikari.ChannelType.GUILD_STAGE)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t("play.error.stage_unsupported", locale="en_US")
    assert ctx.responses[0][0] == "Stage channels aren't supported. Use a regular voice channel."


async def test_bot_in_different_channel_rejected_with_mention(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999, bot_channel_id=888)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    await play_module._handle_play(
        cast(lightbulb.Context, ctx),
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    assert ctx.responses[0][0] == t(
        "play.error.bot_in_other_channel", locale="en_US", channel_id=888
    )
    assert ctx.responses[0][0] == "I'm already playing in <#888>. Join that channel."


# ---------------------------------------------------------------------------
# yt-dlp friendly error mapping (rendered from FetchFailed.key + vars)
# ---------------------------------------------------------------------------


async def test_friendly_yt_dlp_error_renders_from_key(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    with patch.object(
        play_module.ytdlp,
        "resolve_track",
        side_effect=FetchFailed("ytdlp.error.age_restricted"),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    assert ctx.responses[0][0] == t("ytdlp.error.age_restricted", locale="en_US")
    assert ctx.responses[0][0] == "That video is age-restricted and can't be played."


async def test_friendly_message_renders_key_at_locale() -> None:
    exc = FetchFailed("ytdlp.error.age_restricted")
    rendered = play_module._friendly_message(exc, "en_US")
    assert rendered == t("ytdlp.error.age_restricted", locale="en_US")
    assert rendered == "That video is age-restricted and can't be played."


async def test_friendly_message_interpolates_vars() -> None:
    exc = FetchFailed("ytdlp.error.generic_with_detail", detail="boom")
    assert (
        play_module._friendly_message(exc, "en_US")
        == "Could not load that URL. yt-dlp said: `boom`"
    )


def test_fetch_failed_args_contain_en_rendering() -> None:
    """``args[0]`` carries the en-US rendering so log/repr stays readable."""
    exc = FetchFailed("ytdlp.error.private")
    assert str(exc) == "That video is private."
    exc_var = FetchFailed("ytdlp.error.generic_with_detail", detail="boom")
    assert str(exc_var) == "Could not load that URL. yt-dlp said: `boom`"


# ---------------------------------------------------------------------------
# /play with no argument (issue #146): resume reflex
# ---------------------------------------------------------------------------


async def test_no_url_routes_to_resume_when_paused(cache: Any) -> None:
    """``/play`` with empty url + paused player → delegate to ``_handle_resume``.

    Mocks ``_handle_resume`` to keep this test focused on the routing
    decision; resume's own internals are covered in ``test_resume_command``.
    """
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    player.is_playing = True
    player.paused = True

    resume_mock = AsyncMock()
    with patch.object(play_module, "_handle_resume", resume_mock):
        await play_module._handle_play(cast(lightbulb.Context, ctx), "")

    resume_mock.assert_awaited_once_with(ctx)
    # No yt-dlp / track-loading work happened.
    assert ctx.responses == []


@pytest.mark.parametrize("url", ["", "   ", "\t", " \n "])
async def test_no_url_with_nothing_playing_returns_url_hint(url: str, cache: Any) -> None:
    """Bare-or-whitespace ``/play`` + nothing playing → ephemeral URL hint."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    # No player created → ``player_manager.get(guild_id)`` returns None.

    resume_mock = AsyncMock(side_effect=AssertionError("_handle_resume must not run"))
    with patch.object(play_module, "_handle_resume", resume_mock):
        await play_module._handle_play(cast(lightbulb.Context, ctx), url)

    resume_mock.assert_not_awaited()
    assert ctx.responses[0][0] == t("play.error.no_url_and_nothing_playing", locale="en_US")
    assert ctx.responses[0][0] == "Paste a YouTube URL or use /play <url>."
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_no_url_with_playing_not_paused_returns_url_hint(cache: Any) -> None:
    """``/play`` with empty url + playing-not-paused → URL hint (not resume).

    A track is currently playing (not paused). Bare ``/play`` falls through
    to the URL hint rather than no-op-ing or pausing.
    """
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    player.is_playing = True
    player.paused = False

    resume_mock = AsyncMock(side_effect=AssertionError("_handle_resume must not run"))
    with patch.object(play_module, "_handle_resume", resume_mock):
        await play_module._handle_play(cast(lightbulb.Context, ctx), "")

    resume_mock.assert_not_awaited()
    assert ctx.responses[0][0] == t("play.error.no_url_and_nothing_playing", locale="en_US")
    assert ctx.responses[0][1].get("ephemeral") is True


async def test_no_url_in_dm_returns_run_in_server(cache: Any) -> None:
    """DM guard fires before no-arg routing so the run-in-server copy still wins."""
    bot = _FakeBot()
    ctx = _FakeContext(bot, guild_id=None)
    await play_module._handle_play(cast(lightbulb.Context, ctx), "")
    assert ctx.responses[0][0] == t("voice.error.run_in_server", locale="en_US", command="play")
    assert ctx.responses[0][0] == "Run /play in a server."


async def test_no_url_with_no_lavalink_client_returns_url_hint(cache: Any) -> None:
    """Lavalink absent → no paused player can exist → URL hint."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    # No lavalink client installed (lavalink_glue._set_lavalink_client_for_test(None) in fixture).

    resume_mock = AsyncMock(side_effect=AssertionError("_handle_resume must not run"))
    with patch.object(play_module, "_handle_resume", resume_mock):
        await play_module._handle_play(cast(lightbulb.Context, ctx), "")

    resume_mock.assert_not_awaited()
    assert ctx.responses[0][0] == t("play.error.no_url_and_nothing_playing", locale="en_US")


# ---------------------------------------------------------------------------
# Single-track happy + edge paths
# ---------------------------------------------------------------------------


async def test_single_track_success_idle_plays_now(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    track = _track()
    audio_track = _FakeAudioTrack(title=track.title, identifier=track.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )

    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        # Pretend the voice handshake completed instantly.
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    assert bot.update_voice_state_calls == [(111, 999)]
    player = ll.player_manager.players[111]
    assert player.play_called == 1
    assert len(player.added) == 1
    embed = ctx.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)
    assert embed.title == "Queued"
    assert "playing now" in (embed.footer.text if embed.footer else "")
    # Channel + requester surface as inline mention fields so users see
    # which voice channel ryzic joined and who triggered playback.
    field_pairs = {f.name: f.value for f in embed.fields}
    assert field_pairs["Channel"] == "<#999>"
    assert field_pairs["Requested by"] == "<@222>"
    # /play also seeded the last_play_channel for the EventHandler
    # error reporter to land in the right text channel.
    assert lavalink_glue.last_play_channel.get(111) == 555


async def test_single_track_success_with_existing_queue_shows_position(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    # Pre-fill the player queue + mark it as already playing.
    player = ll.player_manager.create(guild_id=111)
    player.queue = [_FakeAudioTrack(title="prev")]
    player.is_playing = True

    track = _track()
    audio_track = _FakeAudioTrack(title=track.title, identifier=track.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )

    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    embed = ctx.responses[0][1]["embed"]
    # Was 1 in queue, now 2 → footer says "position 2 in queue".
    assert "position 2 in queue" in (embed.footer.text if embed.footer else "")
    # Did not call play() again because the player was already playing.
    assert player.play_called == 0


async def test_single_track_queue_full_rejects(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    player.queue = [_FakeAudioTrack(title=f"t{i}") for i in range(500)]

    track = _track()
    with patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    msg = ctx.responses[0][0]
    assert isinstance(msg, str)
    assert msg == t("play.error.queue_full", locale="en_US", count=500, cap=500)
    assert msg == "Queue is full (500/500). Wait for some tracks to finish."
    # Did NOT connect to voice (cap check happens first).
    assert bot.update_voice_state_calls == []


async def test_cache_hit_queue_full_releases_pin(cache: Any) -> None:
    """Cache-hit queue-full path must release the pin acquired by try_hit.

    Otherwise the eviction-blocking pin from try_hit lingers indefinitely
    (M1 §4 release contract): a queue-full /play would silently make
    the cached file un-evictable.
    """
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    player.queue = [_FakeAudioTrack(title=f"t{i}") for i in range(500)]

    track = _track()
    hit = audio_cache.CacheHit(path=Path("/var/cache/x"), track_info=track)
    resolve_track_mock = AsyncMock(side_effect=AssertionError("resolve_track must not run"))
    release_mock = AsyncMock()
    with (
        patch.object(audio_cache.AudioCache, "try_hit", AsyncMock(return_value=hit)),
        patch.object(play_module.ytdlp, "resolve_track", resolve_track_mock),
        patch.object(audio_cache.AudioCache, "release", release_mock),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            f"https://www.youtube.com/watch?v={track.video_id}",
        )

    resolve_track_mock.assert_not_awaited()
    msg = ctx.responses[0][0]
    assert isinstance(msg, str)
    assert msg == t("play.error.queue_full", locale="en_US", count=500, cap=500)
    assert msg == "Queue is full (500/500). Wait for some tracks to finish."
    # The pin acquired by try_hit must be released so the cached file
    # remains evictable.
    release_mock.assert_awaited_once_with(track.video_id)
    # Did NOT connect to voice (cap check happens first).
    assert bot.update_voice_state_calls == []


async def test_voice_handshake_timeout_returns_friendly_error(cache: Any) -> None:
    """Handshake timeout maps to the voice-handshake-failed copy.

    The track must already be loaded by the time we connect (load-first
    order, MEDIUM-1) so the test wires up the full happy load path then
    fails on the handshake.
    """
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    track = _track()
    audio_track = _FakeAudioTrack(title=track.title, identifier=track.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )
    release_mock = AsyncMock()
    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(audio_cache.AudioCache, "release", release_mock),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=False),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    assert ctx.responses[0][0] == t("play.error.voice_handshake_failed", locale="en_US")
    assert ctx.responses[0][0] == (
        "Could not connect to voice. Try again, or make sure I have Connect/Speak in that channel."
    )
    # The pin acquired by get_or_download must be released because we
    # never enqueued the track (handshake failed).
    release_mock.assert_awaited_once_with(track.video_id)


@pytest.mark.parametrize(
    "load_result",
    [
        _FakeLoadResult.empty(),
        _FakeLoadResult(load_type=lavalink.server.LoadType.ERROR, tracks=[], error="boom"),
    ],
    ids=["empty", "load-error"],
)
async def test_lavalink_load_failure_releases_pin(cache: Any, load_result: _FakeLoadResult) -> None:
    """Both EMPTY and ERROR load types must drop the cache pin (M1 §4)."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    track = _track()
    node.get_tracks_results.append(load_result)

    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(audio_cache.AudioCache, "release", AsyncMock()) as release_mock,
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    release_mock.assert_awaited_once_with(track.video_id)
    assert ctx.responses[0][0] == t("play.error.could_not_load_track", locale="en_US")
    assert ctx.responses[0][0] == "Could not load that track. Try a different URL."


# ---------------------------------------------------------------------------
# Playlist URL detection + playlist branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://www.youtube.com/watch?v=abc&list=PL123", True),
        ("https://youtu.be/abc?si=xyz", False),
        # parse_qs accepts arbitrary strings; the helper must not blow up.
        ("not://a-url", False),
        ("", False),
    ],
)
def test_is_playlist_url(url: str, expected: bool) -> None:
    assert play_module._is_playlist_url(url) is expected


async def test_playlist_success(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    entries = [_track(video_id=f"vid{i:08d}") for i in range(3)]
    info = PlaylistInfo(playlist_id="PL12345abcde", title="My PL", entries=entries)
    audio_tracks = [_FakeAudioTrack(title=t.title, identifier=t.video_id) for t in entries]
    for at in audio_tracks:
        node.get_tracks_results.append(
            _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[at])
        )

    with (
        patch.object(
            play_module.playlist_cache,
            "fetch_with_fallback",
            AsyncMock(return_value=(info, 1234567890, False)),
        ),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )

    embed = ctx.responses[0][1]["embed"]
    assert embed.title == "Queued playlist"
    assert "3 tracks" in (embed.description or "")
    player = ll.player_manager.players[111]
    assert len(player.added) == 3
    # play() called exactly once (after first track enqueued).
    assert player.play_called == 1


async def test_playlist_offline_fallback_uses_cache_embed(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    entry = _track()
    info = PlaylistInfo(playlist_id="PL12345abcde", title="Cached", entries=[entry])
    node.get_tracks_results.append(
        _FakeLoadResult(
            load_type=lavalink.server.LoadType.TRACK,
            tracks=[_FakeAudioTrack(title=entry.title, identifier=entry.video_id)],
        )
    )

    with (
        patch.object(
            play_module.playlist_cache,
            "fetch_with_fallback",
            AsyncMock(return_value=(info, 1234567890, True)),
        ),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )

    embed = ctx.responses[0][1]["embed"]
    assert embed.title == "Queued playlist (offline metadata)"


async def test_playlist_empty_returns_friendly(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    info = PlaylistInfo(playlist_id="PL12345abcde", title="empty", entries=[])
    with patch.object(
        play_module.playlist_cache,
        "fetch_with_fallback",
        AsyncMock(return_value=(info, 1234567890, False)),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    assert ctx.responses[0][0] == t("play.error.playlist_empty_or_private", locale="en_US")
    assert ctx.responses[0][0] == "That playlist is empty or private."


async def test_playlist_queue_overflow_rejects(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    # 499 already queued + 2 incoming would breach the 500 cap.
    player.queue = [_FakeAudioTrack() for _ in range(499)]

    info = PlaylistInfo(
        playlist_id="PL12345abcde",
        title="overflow",
        entries=[_track("aaa12345"), _track("bbb12345")],
    )
    with patch.object(
        play_module.playlist_cache,
        "fetch_with_fallback",
        AsyncMock(return_value=(info, 1234567890, False)),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    assert ctx.responses[0][0] == t("play.error.queue_full", locale="en_US", count=499, cap=500)
    assert ctx.responses[0][0] == "Queue is full (499/500). Wait for some tracks to finish."


async def test_playlist_all_tracks_fail_returns_friendly(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    info = PlaylistInfo(
        playlist_id="PL12345abcde",
        title="all-fail",
        entries=[_track("aaa12345"), _track("bbb12345")],
    )
    # Both lavalink loads return EMPTY → both tracks fail.
    node.get_tracks_results.extend([_FakeLoadResult.empty(), _FakeLoadResult.empty()])

    with (
        patch.object(
            play_module.playlist_cache,
            "fetch_with_fallback",
            AsyncMock(return_value=(info, 1234567890, False)),
        ),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(audio_cache.AudioCache, "release", AsyncMock()),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    assert ctx.responses[0][0] == t("play.error.playlist_all_failed", locale="en_US")
    assert ctx.responses[0][0] == "Could not load any tracks from that playlist."


async def test_playlist_yt_dlp_total_failure_returns_friendly(cache: Any) -> None:
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    with patch.object(
        play_module.playlist_cache,
        "fetch_with_fallback",
        AsyncMock(side_effect=FetchFailed("ytdlp.error.private")),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    assert ctx.responses[0][0] == t("ytdlp.error.private", locale="en_US")
    assert ctx.responses[0][0] == "That video is private."


async def test_playlist_voice_handshake_timeout(cache: Any) -> None:
    """Handshake timeout maps to the voice-handshake-failed copy."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    entry = _track()
    info = PlaylistInfo(
        playlist_id="PL12345abcde",
        title="will-time-out",
        entries=[entry],
    )
    audio_track = _FakeAudioTrack(title=entry.title, identifier=entry.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )
    release_mock = AsyncMock()
    with (
        patch.object(
            play_module.playlist_cache,
            "fetch_with_fallback",
            AsyncMock(return_value=(info, 1234567890, False)),
        ),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(audio_cache.AudioCache, "release", release_mock),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=False),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    assert ctx.responses[0][0] == t("play.error.voice_handshake_failed", locale="en_US")
    assert ctx.responses[0][0] == (
        "Could not connect to voice. Try again, or make sure I have Connect/Speak in that channel."
    )
    release_mock.assert_awaited_once_with(entry.video_id)


async def test_load_one_overrides_title_and_author_from_track_info(cache: Any) -> None:
    """Issue #136: lavalink can't read titles from bare-codec files.

    `_load_one` must overwrite the `'Unknown title'` / stale-author values
    coming back from `node.get_tracks` with the real strings yt-dlp
    resolved into `TrackInfo`, so log lines (track-start / track-end /
    track-exception / track-stuck) carry the actual song name.
    """
    ll, node = _ll_with_one_node()
    track_info = _track()
    audio_track = _FakeAudioTrack(title="Unknown title", author="Unknown artist")
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )

    result = await play_module._load_one(
        cache,
        cast(lavalink.Client, ll),
        track_info,
        cached_path=Path("/var/cache/x"),
    )

    assert result is audio_track
    assert audio_track.title == track_info.title
    assert audio_track.author == track_info.uploader


async def test_load_one_handles_node_get_tracks_exception(cache: Any) -> None:
    """A get_tracks() exception must release the cache pin and return None."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    track = _track()
    # Queue an exception in the node's response stack — the load helper
    # must release the cache pin and surface the friendly error.
    node.get_tracks_results.append(RuntimeError("boom"))

    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(audio_cache.AudioCache, "release", AsyncMock()) as release_mock,
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    release_mock.assert_awaited_once_with(track.video_id)
    assert ctx.responses[0][0] == t("play.error.could_not_load_track", locale="en_US")
    assert ctx.responses[0][0] == "Could not load that track. Try a different URL."


# ---------------------------------------------------------------------------
# First-play tip footer (issue #152)
# ---------------------------------------------------------------------------


async def _drive_successful_single_play(
    cache_obj: Any, guild_id: int = 111
) -> tuple[_FakeContext, _FakeLavalinkClient]:
    """Run one successful single-track ``/play`` and return the ctx + ll client.

    Shared helper so the tip-footer tests don't repeat the load mock
    boilerplate. Each call uses a fresh ``_FakeContext`` so the response
    list is isolated. ``guild_id`` plumbs through to the bot's voice
    state so multi-guild tests can drive distinct guilds.
    """
    states: dict[tuple[int, int], _FakeVoiceState | None] = {
        (guild_id, 222): _FakeVoiceState(channel_id=999),
    }
    bot = _FakeBot(states=states, channels={999: _FakeChannel(type=hikari.ChannelType.GUILD_VOICE)})
    ctx = _FakeContext(bot, guild_id=guild_id)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    track = _track()
    audio_track = _FakeAudioTrack(title=track.title, identifier=track.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )
    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    return ctx, ll


def _footer_text(ctx: _FakeContext) -> str:
    embed = ctx.responses[-1][1]["embed"]
    assert isinstance(embed, hikari.Embed)
    return embed.footer.text if embed.footer and embed.footer.text else ""


async def test_first_play_appends_tip_footer(cache: Any) -> None:
    """First successful /play in a fresh guild surfaces the controller-button tip.

    Belt-and-suspenders: assert the tip is present AND byte-identical to
    the catalog rendering, so a future copy edit can't silently drift
    the user-visible string.
    """
    ctx, _ = await _drive_successful_single_play(cache)
    footer = _footer_text(ctx)
    tip = t("play.success.first_play_tip", locale="en_US")
    assert tip in footer
    assert tip == (
        "Tip: the buttons below let you pause / skip / stop & leave without slash commands."
    )
    # Pre-existing footer copy is preserved — the tip is appended, not
    # an overwrite. "playing now" is the build_queued_track_embed footer
    # for an idle-into-play branch.
    assert "playing now" in footer


async def test_second_play_in_same_guild_omits_tip(cache: Any) -> None:
    """Second successful /play in the same guild drops the tip."""
    first_ctx, _ = await _drive_successful_single_play(cache, guild_id=111)
    assert t("play.success.first_play_tip", locale="en_US") in _footer_text(first_ctx)
    second_ctx, _ = await _drive_successful_single_play(cache, guild_id=111)
    footer = _footer_text(second_ctx)
    assert t("play.success.first_play_tip", locale="en_US") not in footer
    # The original footer copy still rides on the second-play embed.
    assert "playing now" in footer


async def test_tip_state_is_per_guild(cache: Any) -> None:
    """A successful /play in guild A doesn't suppress the tip in guild B."""
    ctx_a, _ = await _drive_successful_single_play(cache, guild_id=111)
    assert t("play.success.first_play_tip", locale="en_US") in _footer_text(ctx_a)
    ctx_b, _ = await _drive_successful_single_play(cache, guild_id=222)
    assert t("play.success.first_play_tip", locale="en_US") in _footer_text(ctx_b)


async def test_failed_play_does_not_mark_guild_seen(cache: Any) -> None:
    """Queue-full / FetchFailed paths leave the guild un-marked.

    Otherwise a newcomer whose very first /play happened to land on a
    full queue would silently miss the tip on every subsequent /play.
    """
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx_failed = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    player = ll.player_manager.create(guild_id=111)
    player.queue = [_FakeAudioTrack(title=f"t{i}") for i in range(500)]
    with patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=_track())):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx_failed),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    # The failed /play used the error sentence, not an embed.
    assert isinstance(ctx_failed.responses[0][0], str)
    ctx_after, _ = await _drive_successful_single_play(cache, guild_id=111)
    assert t("play.success.first_play_tip", locale="en_US") in _footer_text(ctx_after)


async def test_playlist_first_play_appends_tip_footer(cache: Any) -> None:
    """Playlist success path also gets the tip on the first /play."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    entries = [_track(video_id=f"vid{i:08d}") for i in range(2)]
    info = PlaylistInfo(playlist_id="PL12345abcde", title="My PL", entries=entries)
    for entry in entries:
        node.get_tracks_results.append(
            _FakeLoadResult(
                load_type=lavalink.server.LoadType.TRACK,
                tracks=[_FakeAudioTrack(title=entry.title, identifier=entry.video_id)],
            )
        )
    with (
        patch.object(
            play_module.playlist_cache,
            "fetch_with_fallback",
            AsyncMock(return_value=(info, 1234567890, False)),
        ),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(return_value=Path("/var/cache/x")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/playlist?list=PL12345abcde",
        )
    footer = _footer_text(ctx)
    assert t("play.success.first_play_tip", locale="en_US") in footer
    # Original playlist footer copy ("requested by …") is preserved.
    assert "requested by" in footer


async def test_yt_dlp_download_failure_per_track_drops_track(cache: Any) -> None:
    """A per-track audio_cache failure must NOT release (download didn't pin)."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, _ = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))
    track = _track()

    with (
        patch.object(play_module.ytdlp, "resolve_track", AsyncMock(return_value=track)),
        patch.object(
            audio_cache.AudioCache,
            "get_or_download",
            AsyncMock(side_effect=FetchFailed("ytdlp.error.generic_with_detail", detail="bonk")),
        ),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    assert ctx.responses[0][0] == t("play.error.could_not_load_track", locale="en_US")
    assert ctx.responses[0][0] == "Could not load that track. Try a different URL."


# ---------------------------------------------------------------------------
# Loader plumbing
# ---------------------------------------------------------------------------


async def test_cached_video_skips_yt_dlp(cache: Any) -> None:
    """Issue #132: cache-first /play insulates from yt-dlp / YouTube breakage."""
    bot = _bot_in_voice_with(user_channel_id=999)
    ctx = _FakeContext(bot)
    ll, node = _ll_with_one_node()
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client, ll))

    track = _track()
    audio_track = _FakeAudioTrack(title=track.title, identifier=track.video_id)
    node.get_tracks_results.append(
        _FakeLoadResult(load_type=lavalink.server.LoadType.TRACK, tracks=[audio_track])
    )

    hit = audio_cache.CacheHit(path=Path("/var/cache/x"), track_info=track)
    resolve_track_mock = AsyncMock(side_effect=AssertionError("resolve_track must not run"))
    get_or_download_mock = AsyncMock(side_effect=AssertionError("get_or_download must not run"))
    with (
        patch.object(audio_cache.AudioCache, "try_hit", AsyncMock(return_value=hit)),
        patch.object(play_module.ytdlp, "resolve_track", resolve_track_mock),
        patch.object(audio_cache.AudioCache, "get_or_download", get_or_download_mock),
        patch.object(
            lavalink_glue,
            "wait_for_voice_ready",
            AsyncMock(return_value=True),
        ),
    ):
        await play_module._handle_play(
            cast(lightbulb.Context, ctx),
            f"https://www.youtube.com/watch?v={track.video_id}",
        )

    resolve_track_mock.assert_not_awaited()
    get_or_download_mock.assert_not_awaited()
    embed = ctx.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)
    assert embed.title == "Queued"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=xyz", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", "dQw4w9WgXcQ"),
        # Unsupported / malformed shapes fall through to None.
        ("https://www.youtube.com/", None),
        ("https://www.youtube.com/playlist?list=PL123", None),
        ("https://www.youtube.com/watch?v=../escape", None),
        ("not-a-url", None),
        ("", None),
    ],
)
def test_parse_video_id(url: str, expected: str | None) -> None:
    assert ytdlp.parse_video_id(url) == expected


def test_loader_registered_play_command() -> None:
    # Smoke check: the module exposes a Loader holding the Play command.
    # The metaclass populates _command_data with the right name.
    assert play_module.Play._command_data.name == "play"
    assert play_module.Play._command_data.description == t(
        "play.command.description", locale="en_US"
    )
    assert play_module.Play._command_data.description == "Queue a YouTube track or playlist URL."
    # 1 string option named url, optional (default=""), max 500 chars.
    opt = play_module.Play._command_data.options["url"]
    assert opt.default == ""
    assert opt.max_length == 500
    assert opt.description == t("play.param.url.description", locale="en_US")
    assert opt.description == "YouTube video or playlist URL."
