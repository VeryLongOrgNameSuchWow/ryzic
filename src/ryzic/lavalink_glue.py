"""Bridge between hikari's gateway and lavalink.py's player runtime.

The library ships no hikari adapter. We translate hikari voice events into
the discord.py-shaped dicts that ``lavalink.Client.voice_update_handler``
expects, and we own the per-guild state that the player API does not track:
the last text channel that issued ``/play`` (so error messages have somewhere
to land), the cancellable post-queue auto-leave timers (configurable via
``RYZIC_AUTOLEAVE_SECONDS``), and the ``asyncio.Event`` per guild that lets
``/play`` block on the voice handshake before calling ``player.play()``.

State lives at module scope rather than in a ``GuildState`` registry: there
are only three fields, none cross-reference each other, and an extra layer
would be pure ceremony. Tests must use ``_reset_state_for_test`` /
``_set_lavalink_client_for_test`` to keep that state from leaking across
cases — the ``_reset_state`` autouse fixture in ``tests/test_lavalink_bridge``
is the canonical example.

Subtleties worth knowing about:

* ``TrackEndEvent`` MUST NOT call ``player.play()``. The default player
  advances the queue itself; calling ``play()`` from the end-of-track hook
  trips Lavalink.py's open assertion bug (Devoxin/Lavalink.py#153).

* The voice handshake races: ``bot.update_voice_state`` returns immediately,
  but Lavalink only learns about the connection once both
  ``VoiceStateUpdateEvent`` (for the bot itself) and ``VoiceServerUpdateEvent``
  arrive at this module and we forward them. ``player.play()`` fired before
  that completes silently does nothing. ``wait_for_voice_ready`` is the
  guard.

* ``event.endpoint`` is the hikari property that prepends ``wss://``. We
  strip the scheme with ``removeprefix`` rather than ``[6:]`` so a future
  hikari change to scheme handling cannot silently corrupt the host string.

* PR6a's ``/play`` consumes lavalink via ``get_lavalink_client()`` and treats
  ``None`` as "Audio service is down. Try again in a minute." (M1 §3). We
  deliberately do NOT register a lightbulb DI factory: linkd wraps factory
  exceptions in ``DependencyNotSatisfiableException`` so a typed not-ready
  exception would never reach the command boundary.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import cast

import hikari
import lavalink
from lavalink.common import VoiceServerUpdatePayload, VoiceStateUpdatePayload

from . import audio_cache, config, track_history, ux

_log = logging.getLogger(__name__)


DEFAULT_AUTO_LEAVE_SECONDS = 300
VOICE_READY_TIMEOUT_SECONDS = 5.0

# Mutated once at startup by ``register_listeners`` from ``cfg.auto_leave_seconds``.
# Module-level by design (mirrors the ``_ll_client`` singleton): the auto-leave
# task is fire-and-forget by ``EventHandler.on_queue_end``, so the value has to
# be reachable without lugging cfg through every event hook. ``0`` means the
# operator opted out of the timer entirely.
_auto_leave_seconds: int = DEFAULT_AUTO_LEAVE_SECONDS

# Defense in depth: only forward voice endpoints that look like Discord's voice
# infrastructure. Anything else (a hypothetical compromised gateway) is dropped
# silently with a warning rather than handed to lavalink for connection.
_DISCORD_ENDPOINT_RE = re.compile(r"^[a-z0-9-]+\.discord\.media(:\d+)?$")

# Strips Discord markdown control chars so server-supplied error text cannot
# break out of formatting or smuggle pings.
_MARKDOWN_STRIP_RE = re.compile(r"[`*_~|\[\]]")


# Per-guild state. Module-level by design (see module docstring).
last_play_channel: dict[int, int] = {}
auto_leave_tasks: dict[int, asyncio.Task[None]] = {}
_voice_ready_events: dict[int, asyncio.Event] = {}


# Singleton lavalink client. Constructed on the first ``ShardReadyEvent``
# (needs the bot's user id).
_ll_client: lavalink.Client | None = None


def get_lavalink_client() -> lavalink.Client | None:
    """Return the active lavalink client, or ``None`` before bootstrap.

    PR6a's ``/play`` is the primary consumer: ``None`` maps to the
    "Audio service is down. Try again in a minute." path from M1 §3.
    """
    return _ll_client


async def wait_for_voice_ready(guild_id: int, timeout: float = VOICE_READY_TIMEOUT_SECONDS) -> bool:
    """Block until our own voice state for ``guild_id`` has been forwarded.

    Returns ``True`` if the handshake completed in time, ``False`` on
    timeout. Callers should treat ``False`` as "abort the play attempt and
    surface a friendly error" rather than retrying — the gateway timed out,
    not a transient race.

    The event is cleared up-front so a stale "ready" from a prior session
    cannot return True instantly: a channel move from A → B (both non-None)
    leaves the old event set, but lavalink.py won't see the new
    VOICE_SERVER_UPDATE for B until the setter re-fires.
    """
    event = _voice_ready_events.setdefault(guild_id, asyncio.Event())
    event.clear()
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


def _reset_voice_ready(guild_id: int) -> None:
    """Forget any prior handshake; the next ``/play`` must wait again."""
    _voice_ready_events.pop(guild_id, None)


def _cancel_auto_leave(guild_id: int) -> None:
    task = auto_leave_tasks.pop(guild_id, None)
    if task is not None and not task.done():
        task.cancel()


def _start_auto_leave(bot: hikari.GatewayBot, guild_id: int) -> None:
    """Schedule the disconnect timer for ``guild_id``.

    Replaces any existing timer for the same guild so the user always gets
    a fresh window after the most recent ``QueueEndEvent``. When the
    operator has set ``RYZIC_AUTOLEAVE_SECONDS=0`` the timer is disabled
    entirely — any existing timer is still cancelled (so a mid-flight
    config change doesn't leave a stale 300s task armed) and no
    replacement is scheduled.
    """
    _cancel_auto_leave(guild_id)
    if _auto_leave_seconds <= 0:
        return
    auto_leave_tasks[guild_id] = asyncio.create_task(
        _auto_leave(bot, guild_id, _auto_leave_seconds),
        name=f"ryzic-auto-leave-{guild_id}",
    )


async def _auto_leave(bot: hikari.GatewayBot, guild_id: int, seconds: int) -> None:
    await asyncio.sleep(seconds)

    # Self-pop is best-effort; a concurrent _cancel_auto_leave / _start_auto_leave
    # may have already replaced the entry, so only clear ourselves out.
    if auto_leave_tasks.get(guild_id) is asyncio.current_task():
        del auto_leave_tasks[guild_id]

    client = _ll_client
    if client is None:
        return
    player = client.player_manager.get(guild_id)
    if player is None or not player.is_connected:
        return

    try:
        await bot.update_voice_state(guild_id, None)
    except Exception:
        _log.exception("auto-leave: failed to disconnect from guild %d", guild_id)

    _reset_voice_ready(guild_id)
    await _send_to_last_play_channel(
        bot, guild_id, f"Idle for {_format_idle_duration(seconds)} — disconnecting."
    )


def _format_idle_duration(seconds: int) -> str:
    """Render the idle duration in the most natural unit for the user-facing message.

    Whole minutes (300, 60, 600, ...) render as ``"5 minutes"``; everything
    else renders as seconds. Prevents ``"Idle for 300 seconds"`` for the
    default while still being correct for ``RYZIC_AUTOLEAVE_SECONDS=45``.
    """
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    return "1 second" if seconds == 1 else f"{seconds} seconds"


async def _send_to_last_play_channel(bot: hikari.GatewayBot, guild_id: int, content: str) -> None:
    channel_id = last_play_channel.get(guild_id)
    if channel_id is None:
        return
    try:
        await bot.rest.create_message(channel_id, content)
    except hikari.NotFoundError:
        # Channel deleted (or bot lost access) — drop the stale mapping so we
        # stop re-attempting until the next /play repopulates it.
        last_play_channel.pop(guild_id, None)
        _log.warning(
            "last_play_channel %d for guild %d is gone; forgetting it",
            channel_id,
            guild_id,
        )
    except hikari.HikariError:
        _log.warning(
            "could not post to last_play_channel %d for guild %d",
            channel_id,
            guild_id,
        )


def _track_title(track: lavalink.AudioTrack | None) -> str:
    return track.title if track is not None else "<unknown>"


async def _release_track(track: lavalink.AudioTrack | None) -> None:
    """Drop the audio cache pin for ``track`` if a cache + identifier exist.

    No-ops when the cache hasn't been bootstrapped yet (test harnesses
    without a cache, early shutdown order) or when the track has no
    identifier (Lavalink occasionally surfaces ``None`` after a
    failed-decode TrackEndEvent — release would do nothing useful).
    """
    if track is None:
        return
    cache = audio_cache.get_audio_cache()
    if cache is None:
        return
    identifier = getattr(track, "identifier", None)
    if not identifier:
        return
    # ``LocalAudioSourceManager`` populates ``AudioTrackInfo.identifier``
    # with the file path we passed to ``node.get_tracks(...)`` — e.g.
    # ``/var/cache/ryzic/audio/dQ/dQw4w9WgXcQ.audio``. The cache pins
    # by ``video_id``, so we strip the path components to recover it
    # (file stem == video_id by construction in ``audio_cache._audio_path``).
    video_id = Path(identifier).stem
    try:
        await cache.release(video_id)
    except Exception:
        # Releasing should never raise; log + swallow rather than letting
        # the lavalink event hook take down the player loop.
        _log.exception("failed to release audio cache entry %s", video_id)


# EndReason values that mean "the user actually heard this track".
# FINISHED = natural end. REPLACED = the next track took over via
# ``player.play(track=...)`` (e.g. ``/skip``). LOAD_FAILED / STOPPED /
# CLEANUP are excluded — failures were never heard, ``/leave`` winds the
# session down explicitly, and Lavalink cleanup events don't correspond
# to user-facing playback. A tuple (rather than a frozenset) because
# ``lavalink.server.EndReason`` overrides ``__eq__`` without preserving
# ``__hash__``, so the values are not hashable.
_HEARD_END_REASONS: tuple[lavalink.server.EndReason, ...] = (
    lavalink.server.EndReason.FINISHED,
    lavalink.server.EndReason.REPLACED,
)


def _record_history(
    guild_id: int,
    track: lavalink.AudioTrack | None,
    reason: lavalink.server.EndReason,
) -> None:
    """Append ``track`` to ``guild_id``'s history if the user actually heard it.

    Reads the original :class:`~ryzic.ytdlp.TrackInfo` off the lavalink
    AudioTrack via :func:`ux.get_track_info`. When metadata is absent
    (a future code path that bypasses ``attach_track_info``) we drop
    the entry rather than fabricate one — history is a UX surface, not
    a play-count store, so a missing-metadata gap is preferable to a
    half-populated row.
    """
    if track is None or reason not in _HEARD_END_REASONS:
        return
    info = ux.get_track_info(track)
    if info is None:
        return
    track_history.record(guild_id, info)


async def clear_queue_releasing(player: lavalink.DefaultPlayer) -> None:
    """Release audio cache pins for every queued track, then clear the queue.

    ``TrackEndEvent`` fires (and ``_release_track`` runs) only for tracks
    that actually played; queue-clear paths that drop unplayed tracks
    leave their pins held forever. Centralising the release walk here
    keeps the three callers (``/leave``, voice 4014 disconnect, node
    disconnect) honest about the contract.

    Snapshot the queue and clear it BEFORE awaiting any release: a
    concurrent ``/play`` (or other ``player.queue.add`` caller) that
    lands between ``await`` boundaries on the snapshot would otherwise
    have its newly-queued track silently wiped by the trailing
    ``queue.clear()``, leaking its pin. Clear-then-release leaves a
    fresh queue for any racing producer.
    """
    tracks = list(player.queue)
    player.queue.clear()
    for track in tracks:
        await _release_track(track)


def _safe_error_text(text: str | None) -> str:
    """Sanitise server-supplied error text before posting to a public channel.

    Lavalink's TrackException ``cause``/``message`` strings can include JVM
    stack traces, server-side filesystem paths, internal hostnames, and
    signed stream URLs. Strip Discord markdown chars so they cannot escape
    formatting, take only the first line, cap at 200 chars.
    """
    if not text:
        return "unknown error"
    cleaned = _MARKDOWN_STRIP_RE.sub("", text)
    first_line = cleaned.splitlines()[0] if cleaned else ""
    return first_line[:200] or "unknown error"


class EventHandler:
    """Lavalink event hooks.

    Registered once via ``client.add_event_hooks(EventHandler(bot))`` after
    the lavalink client is constructed. The instance keeps a reference to
    the bot so ``REST`` calls (channel posts, voice disconnects) are
    available without smuggling the bot through globals.
    """

    def __init__(self, bot: hikari.GatewayBot) -> None:
        self._bot = bot

    @lavalink.listener(lavalink.TrackStartEvent)
    async def on_track_start(self, event: lavalink.TrackStartEvent) -> None:
        guild_id = event.player.guild_id
        _cancel_auto_leave(guild_id)
        _log.info("guild=%d track-start title=%r", guild_id, _track_title(event.track))
        # Lazy-import sidesteps the ``now_playing → lavalink_glue`` cycle.
        from . import now_playing

        await now_playing.upsert_for_track_start(self._bot, guild_id)

    @lavalink.listener(lavalink.TrackEndEvent)
    async def on_track_end(self, event: lavalink.TrackEndEvent) -> None:
        guild_id = event.player.guild_id
        _log.info(
            "guild=%d track-end title=%r reason=%s",
            guild_id,
            _track_title(event.track),
            event.reason,
        )
        _record_history(guild_id, event.track, event.reason)
        await _release_track(event.track)

        # NEVER call player.play() here; the default player auto-advances
        # the queue and a manual play() races into Lavalink.py#153.

    @lavalink.listener(lavalink.TrackExceptionEvent)
    async def on_track_exception(self, event: lavalink.TrackExceptionEvent) -> None:
        guild_id = event.player.guild_id
        title = _track_title(event.track)
        _log.warning(
            "guild=%d track-exception title=%r severity=%s cause=%s",
            guild_id,
            title,
            event.severity,
            event.cause,
        )
        await _release_track(event.track)
        # ``event.cause`` is a JVM stack trace; ``message`` is a short cause
        # description. Prefer ``message`` and never the full ``cause`` —
        # both are sanitised regardless.
        detail = _safe_error_text(event.message)
        await _send_to_last_play_channel(
            self._bot,
            guild_id,
            f"Track **{_safe_error_text(title)}** failed: {detail}. Skipping.",
        )

    @lavalink.listener(lavalink.TrackStuckEvent)
    async def on_track_stuck(self, event: lavalink.TrackStuckEvent) -> None:
        # Mitigates Devoxin/Lavalink.py#144: the default player otherwise
        # sits forever on the stuck track waiting for a TrackEndEvent that
        # never comes.
        guild_id = event.player.guild_id
        title = _track_title(event.track)
        _log.warning(
            "guild=%d track-stuck title=%r threshold_ms=%d",
            guild_id,
            title,
            event.threshold,
        )
        try:
            await cast(lavalink.DefaultPlayer, event.player).skip()
        except Exception:
            _log.exception("guild=%d failed to skip stuck track", guild_id)
        await _send_to_last_play_channel(
            self._bot,
            guild_id,
            f"Track **{_safe_error_text(title)}** got stuck and was skipped.",
        )

    @lavalink.listener(lavalink.QueueEndEvent)
    async def on_queue_end(self, event: lavalink.QueueEndEvent) -> None:
        guild_id = event.player.guild_id
        if _auto_leave_seconds <= 0:
            _log.info(
                "guild=%d queue-end; auto-leave disabled (RYZIC_AUTOLEAVE_SECONDS=0)",
                guild_id,
            )
        else:
            _log.info(
                "guild=%d queue-end; arming %ds auto-leave",
                guild_id,
                _auto_leave_seconds,
            )
        _start_auto_leave(self._bot, guild_id)
        from . import now_playing

        await now_playing.refresh(self._bot, guild_id)

    @lavalink.listener(lavalink.WebSocketClosedEvent)
    async def on_websocket_closed(self, event: lavalink.WebSocketClosedEvent) -> None:
        # 4014 = "Disconnected" — channel deleted, kicked from voice, or the
        # voice region was migrated. Any of these mean our queue is dead.
        guild_id = event.player.guild_id
        _log.warning(
            "guild=%d voice-ws-closed code=%d reason=%r by_remote=%s",
            guild_id,
            event.code,
            event.reason,
            event.by_remote,
        )
        if event.code != 4014:
            return
        await clear_queue_releasing(cast(lavalink.DefaultPlayer, event.player))
        _cancel_auto_leave(guild_id)
        _reset_voice_ready(guild_id)
        await _send_to_last_play_channel(
            self._bot, guild_id, "Voice connection lost. Queue cleared."
        )
        from . import now_playing

        await now_playing.teardown(self._bot, guild_id)

    @lavalink.listener(lavalink.NodeDisconnectedEvent)
    async def on_node_disconnected(self, event: lavalink.NodeDisconnectedEvent) -> None:
        _log.warning(
            "node=%s disconnected code=%s reason=%r",
            event.node.name,
            event.code,
            event.reason,
        )
        client = _ll_client
        if client is None:
            return
        from . import now_playing

        notified: set[int] = set()
        for player in list(client.player_manager.values()):
            guild_id = player.guild_id
            await clear_queue_releasing(cast(lavalink.DefaultPlayer, player))
            _cancel_auto_leave(guild_id)
            _reset_voice_ready(guild_id)
            await now_playing.teardown(self._bot, guild_id)
            if guild_id in notified:
                continue
            notified.add(guild_id)
            await _send_to_last_play_channel(
                self._bot,
                guild_id,
                "Audio service disconnected. Reconnecting...",
            )

    @lavalink.listener(lavalink.NodeConnectedEvent)
    async def on_node_connected(self, event: lavalink.NodeConnectedEvent) -> None:
        _log.info("node=%s connected", event.node.name)


def _bridge_voice_server_payload(
    event: hikari.VoiceServerUpdateEvent,
) -> VoiceServerUpdatePayload:
    """Build the payload ``voice_update_handler`` expects from a server event.

    Pulled out so tests can exercise the translation without spinning up a
    real lavalink client.
    """
    endpoint = event.endpoint
    return cast(
        VoiceServerUpdatePayload,
        {
            "t": "VOICE_SERVER_UPDATE",
            "d": {
                "guild_id": str(event.guild_id),
                "endpoint": endpoint.removeprefix("wss://") if endpoint else None,
                "token": event.token,
            },
        },
    )


def _bridge_voice_state_payload(
    event: hikari.VoiceStateUpdateEvent,
) -> VoiceStateUpdatePayload:
    state = event.state
    return cast(
        VoiceStateUpdatePayload,
        {
            "t": "VOICE_STATE_UPDATE",
            "d": {
                "guild_id": str(state.guild_id),
                "user_id": str(state.user_id),
                "channel_id": (str(state.channel_id) if state.channel_id is not None else None),
                "session_id": state.session_id,
            },
        },
    )


def _is_valid_discord_endpoint(endpoint: str | None) -> bool:
    return endpoint is not None and _DISCORD_ENDPOINT_RE.match(endpoint) is not None


async def _on_voice_server_update(event: hikari.VoiceServerUpdateEvent) -> None:
    payload = _bridge_voice_server_payload(event)
    endpoint = payload["d"]["endpoint"]
    if not _is_valid_discord_endpoint(endpoint):
        _log.warning(
            "guild=%d dropping voice-server update with non-Discord endpoint %r",
            event.guild_id,
            endpoint,
        )
        return
    if _ll_client is None:
        return
    await _ll_client.voice_update_handler(payload)


async def _on_voice_state_update(event: hikari.VoiceStateUpdateEvent) -> None:
    # Handshake bookkeeping runs BEFORE the lavalink short-circuit so an
    # early voice state for our own user during the bootstrap window still
    # marks the guild ready (see MEDIUM-2 in PR3-review.md).
    _track_own_voice_state(event)

    if _ll_client is None:
        return
    await _ll_client.voice_update_handler(_bridge_voice_state_payload(event))


def _track_own_voice_state(event: hikari.VoiceStateUpdateEvent) -> None:
    # Resolve the bot's own user via ``get_me`` if the app supports it.
    # Duck-typed so tests can substitute a minimal app without subclassing
    # ``hikari.GatewayBot``.
    get_me = getattr(event.app, "get_me", None)
    bot_user = get_me() if callable(get_me) else None
    if bot_user is None or event.state.user_id != bot_user.id:
        return

    if event.state.channel_id is None:
        _reset_voice_ready(event.state.guild_id)
    else:
        _voice_ready_events.setdefault(event.state.guild_id, asyncio.Event()).set()


async def _on_guild_leave(event: hikari.GuildLeaveEvent) -> None:
    """Tear down per-guild state when the bot is kicked / leaves a guild."""
    guild_id = event.guild_id
    _cancel_auto_leave(guild_id)
    last_play_channel.pop(guild_id, None)
    _voice_ready_events.pop(guild_id, None)
    from . import now_playing

    # No REST teardown — the bot has just lost its messages-write
    # permission to the guild, so an edit would 403. Drop the local
    # record so we don't try again later.
    now_playing._controllers.pop(guild_id, None)
    client = _ll_client
    if client is not None:
        try:
            player = cast(lavalink.DefaultPlayer | None, client.player_manager.get(guild_id))
            if player is not None:
                await clear_queue_releasing(player)
            await client.player_manager.destroy(guild_id)
        except Exception:
            _log.exception("guild=%d failed to destroy player on guild-leave", guild_id)


async def _on_shard_ready(
    bot: hikari.GatewayBot, cfg: config.Config, event: hikari.ShardReadyEvent
) -> None:
    """Construct the lavalink client + add the configured node, once.

    ``ShardReadyEvent`` fires per-shard reconnect; we keep the existing
    client across reconnects to preserve player state.
    """
    global _ll_client
    if _ll_client is not None:
        return
    _ll_client = _build_lavalink_client(bot, cfg, event.my_user.id)


def _build_lavalink_client(
    bot: hikari.GatewayBot, cfg: config.Config, user_id: int
) -> lavalink.Client:
    client = lavalink.Client(user_id)
    client.add_node(
        host=cfg.lavalink_host,
        port=cfg.lavalink_port,
        password=cfg.lavalink_password,
        region="us",
        # The explicit name keeps host:port out of the default
        # f"{region}-{host}:{port}" so log lines (which include node.name)
        # don't leak the address to anyone reading the bot's stdout.
        name="ryzic-default",
    )
    client.add_event_hooks(EventHandler(bot))
    _log.info("lavalink client constructed: host=%s port=%d", cfg.lavalink_host, cfg.lavalink_port)
    return client


def register_listeners(bot: hikari.GatewayBot, cfg: config.Config) -> None:
    """Subscribe the voice bridge + node bootstrap listeners onto ``bot``.

    Also installs the per-process auto-leave window from
    ``cfg.auto_leave_seconds`` so ``EventHandler.on_queue_end`` (which has
    no cfg in scope) can read it without indirection.

    Idempotent across calls is NOT guaranteed; call once at startup.
    """
    global _auto_leave_seconds
    _auto_leave_seconds = cfg.auto_leave_seconds

    async def _shard_ready(event: hikari.ShardReadyEvent) -> None:
        await _on_shard_ready(bot, cfg, event)

    bot.subscribe(hikari.ShardReadyEvent, _shard_ready)
    bot.subscribe(hikari.VoiceServerUpdateEvent, _on_voice_server_update)
    bot.subscribe(hikari.VoiceStateUpdateEvent, _on_voice_state_update)
    bot.subscribe(hikari.GuildLeaveEvent, _on_guild_leave)


def _set_lavalink_client_for_test(client: lavalink.Client | None) -> None:
    """Test-only: install a stand-in lavalink client (or clear it)."""
    global _ll_client
    _ll_client = client


def _set_auto_leave_seconds_for_test(seconds: int) -> None:
    """Test-only: override the per-process auto-leave window."""
    global _auto_leave_seconds
    _auto_leave_seconds = seconds


def _reset_state_for_test() -> None:
    global _auto_leave_seconds
    for task in auto_leave_tasks.values():
        if not task.done():
            task.cancel()
    last_play_channel.clear()
    auto_leave_tasks.clear()
    _auto_leave_seconds = DEFAULT_AUTO_LEAVE_SECONDS
    _voice_ready_events.clear()
