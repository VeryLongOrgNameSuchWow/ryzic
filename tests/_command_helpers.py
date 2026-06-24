"""Shared fakes for ``commands/*.py`` unit tests.

Each command test file would otherwise re-declare the same slim
hikari/lightbulb/lavalink stand-ins; centralising them here keeps the
five PR6b test files focused on the per-command branches they exercise.

The fakes are intentionally minimal — they cover only the surface the
commands actually touch (``ctx.respond``, ``bot.cache.get_voice_state``,
``ll_client.player_manager.get``, ``player.{set_pause, skip, stop,
queue, current, paused, is_playing, is_connected, position}``). When a
test needs additional behaviour, attach it on the instance rather than
extending the base classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import lavalink
import lightbulb

from ryzic import lavalink_glue, ux
from ryzic.ytdlp import TrackInfo


@dataclass
class FakeUser:
    id: int
    username: str = "alice"


@dataclass
class FakeVoiceState:
    channel_id: int | None


class FakeCache:
    def __init__(self, states: Mapping[tuple[int, int], FakeVoiceState | None]) -> None:
        self._states = states

    def get_voice_state(self, guild_id: int, user_id: int) -> FakeVoiceState | None:
        return self._states.get((guild_id, user_id))


class FakeBot:
    """Slim ``hikari.GatewayBot`` stand-in covering the surface commands use."""

    def __init__(
        self,
        bot_user_id: int = 10,
        states: Mapping[tuple[int, int], FakeVoiceState | None] | None = None,
    ) -> None:
        self._me = FakeUser(bot_user_id)
        self.cache = FakeCache(states or {})
        self.update_voice_state_calls: list[tuple[int, int | None]] = []

    def get_me(self) -> FakeUser:
        return self._me

    async def update_voice_state(
        self,
        guild_id: int,
        channel_id: int | None,
        *,
        self_deaf: bool = False,
    ) -> None:
        self.update_voice_state_calls.append((guild_id, channel_id))


class FakeLightbulbClient:
    def __init__(self, app: FakeBot) -> None:
        self.app = app


class FakeInteraction:
    """Stand-in for ``ctx.interaction`` so ``locale_for_*`` resolvers run.

    ``ryzic.i18n.locale_for_ephemeral`` / ``locale_for_public`` access
    ``ctx.interaction.locale`` / ``guild_locale`` via ``getattr``-with-
    default; this class exposes neither attribute so the resolvers fall
    back to ``en_US`` (matching the test expectation that English copy
    is asserted byte-identical).
    """


@dataclass
class FakeCommandData:
    name: str = "skip"


class FakeContext:
    def __init__(
        self,
        bot: FakeBot,
        guild_id: int | None = 111,
        user_id: int = 222,
        channel_id: int = 555,
        command_name: str = "skip",
    ) -> None:
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.channel_id = channel_id
        self.client = FakeLightbulbClient(bot)
        self.interaction = FakeInteraction()
        self.command_data = FakeCommandData(name=command_name)
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.defer_calls: int = 0

    async def respond(self, content: Any = None, **kwargs: Any) -> None:
        self.responses.append((content, kwargs))

    async def defer(self) -> None:
        self.defer_calls += 1


@dataclass
class FakeAudioTrack:
    """Slim AudioTrack stand-in covering the duck shape commands read."""

    title: str = "Some Song"
    identifier: str = "abc123"
    duration: int = 213_000
    uri: str = "/var/cache/x"
    requester: int = 222
    extra: dict[str, Any] = field(default_factory=dict)


class RecordingCache:
    """Captures ``release`` calls so tests can assert pin release on queue clear.

    Used by tests that exercise ``lavalink_glue.clear_queue_releasing`` and
    its callers (``/leave``, voice 4014 disconnect, node disconnect) — the
    real :class:`~ryzic.audio_cache.AudioCache` is sqlite-backed and
    overkill for assertions about the release walk's contract.
    """

    def __init__(self) -> None:
        self.released: list[str] = []

    async def release(self, video_id: str) -> None:
        self.released.append(video_id)


class FakePlayer:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.queue: list[FakeAudioTrack] = []
        self.current: FakeAudioTrack | None = None
        self.paused: bool = False
        self.is_connected: bool = False
        self.position: int = 0
        # call counters
        self.set_pause_calls: list[bool] = []
        self.skip_calls: int = 0
        self.stop_calls: int = 0
        self.seek_calls: list[int] = []

    @property
    def is_playing(self) -> bool:
        # Intentionally mirrors real ``lavalink.DefaultPlayer.is_playing``
        # (``is_connected and current is not None`` — no ``paused`` term).
        # Do NOT "fix" this to return False when paused; that would diverge
        # from lavalink semantics and mask guard-policy regressions.
        return self.is_connected and self.current is not None

    async def set_pause(self, pause: bool) -> None:
        self.set_pause_calls.append(pause)
        self.paused = pause

    async def skip(self) -> None:
        self.skip_calls += 1
        # Mimic DefaultPlayer.skip: pop the next track off the queue
        # into ``current``, or clear ``current`` if the queue is empty.
        if self.queue:
            self.current = self.queue.pop(0)
        else:
            self.current = None

    async def stop(self) -> None:
        self.stop_calls += 1
        self.current = None

    async def seek(self, position_ms: int) -> None:
        # Mirrors real ``lavalink.DefaultPlayer.seek``: records the call but
        # does not update ``position``. Tests that need a specific rendered
        # position set ``player.position`` directly.
        self.seek_calls.append(position_ms)


class FakePlayerManager:
    def __init__(self) -> None:
        self.players: dict[int, FakePlayer] = {}

    def get(self, guild_id: int) -> FakePlayer | None:
        return self.players.get(guild_id)

    def create(self, guild_id: int, **kwargs: Any) -> FakePlayer:
        if guild_id not in self.players:
            self.players[guild_id] = FakePlayer(guild_id)
        return self.players[guild_id]


class FakeLavalinkClient:
    def __init__(self) -> None:
        self.player_manager = FakePlayerManager()


def install_lavalink_client(client: FakeLavalinkClient | None) -> None:
    """Inject ``client`` (or clear) into the lavalink_glue singleton."""
    lavalink_glue._set_lavalink_client_for_test(cast(lavalink.Client | None, client))


def both_in_voice(
    channel_id: int = 999,
    *,
    user_id: int = 222,
    bot_user_id: int = 10,
    guild_id: int = 111,
) -> FakeBot:
    """Build a ``FakeBot`` with both bot and user in the same voice channel."""
    return FakeBot(
        bot_user_id=bot_user_id,
        states={
            (guild_id, bot_user_id): FakeVoiceState(channel_id=channel_id),
            (guild_id, user_id): FakeVoiceState(channel_id=channel_id),
        },
    )


def context_for(bot: FakeBot, **kwargs: Any) -> lightbulb.Context:
    """Wrap :class:`FakeContext` in a ``lightbulb.Context`` cast for ty."""
    return cast(lightbulb.Context, FakeContext(bot, **kwargs))


def make_track_info(
    *,
    video_id: str = "dQw4w9WgXcQ",
    title: str = "Test Song",
    uploader: str = "Tester",
    duration_ms: int = 180_000,
    url: str | None = None,
) -> TrackInfo:
    """Build a :class:`TrackInfo` with realistic defaults for command tests."""
    return TrackInfo(
        video_id=video_id,
        url=url or f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        uploader=uploader,
        duration_ms=duration_ms,
    )


def make_track_with_info(
    info: TrackInfo | None = None,
    *,
    requester: int = 222,
    **info_overrides: Any,
) -> FakeAudioTrack:
    """Build a :class:`FakeAudioTrack` with an attached :class:`TrackInfo`.

    Pass ``info`` to attach an existing instance, or pass keyword args
    forwarded to :func:`make_track_info` to construct one inline.
    """
    if info is None:
        info = make_track_info(**info_overrides)
    track = FakeAudioTrack(title=info.title, requester=requester)
    ux.attach_track_info(cast(Any, track), info)
    return track
