"""``/play`` slash command (M1 §3).

Single source of truth for all the URL → playback wiring. The handler:

1. Defers the response (public, since success embeds are public).
2. Validates the URL via :func:`ryzic.url_validator.is_supported_url`.
3. Detects whether the URL is a playlist (``list=`` query param) and
   resolves metadata via either :func:`ryzic.ytdlp.resolve_track` or
   :func:`ryzic.playlist_cache.fetch_with_fallback` accordingly.
4. Verifies voice prerequisites: invoker is in a voice channel, the
   channel is not a stage, the bot is not pinned in another channel,
   the lavalink + audio cache singletons are bootstrapped, the queue
   has capacity for the new tracks.
5. Connects via ``bot.update_voice_state`` and waits for the voice
   handshake (handled by ``lavalink_glue.wait_for_voice_ready``) before
   touching the player.
6. Per track: downloads via :class:`ryzic.audio_cache.AudioCache`,
   loads via Lavalink's ``LocalAudioSourceManager``, calls
   ``player.add``. Releases the audio cache pin on load failure so
   eviction stays unblocked.
7. Calls ``player.play()`` only when the player wasn't already playing.

All friendly error sentences come VERBATIM from the wrapper exceptions
(M1 §6 fixed those to be user-presentable). The handler never rewrites
them; that contract keeps the failure surface understandable.
"""

from __future__ import annotations

import logging
from typing import cast
from urllib.parse import parse_qs, urlparse

import hikari
import lavalink
import lightbulb

from .. import audio_cache, lavalink_glue, playlist_cache, ux, ytdlp
from ..errors import FetchFailed, InvalidVideoID
from ..url_validator import is_supported_url

_log = logging.getLogger(__name__)

loader = lightbulb.Loader()

# Per M1 §3: cap the per-guild queue at 500 tracks. Enforced before
# ``player.add`` so a giant playlist cannot silently displace the cap.
_QUEUE_CAP: int = 500

# Voice handshake timeout — also enforced inside lavalink_glue, but the
# command-side guard exists so we can map "the gateway never confirmed"
# to a friendly ephemeral rather than an opaque ``RuntimeError``.
_VOICE_READY_TIMEOUT_S: float = 5.0


@loader.command
class Play(
    lightbulb.SlashCommand,
    name="play",
    description="Queue a YouTube track or playlist URL.",
    # ``contexts=[GUILD]`` is the lightbulb v3 / Discord-API replacement
    # for the v2 ``dm_enabled=False``: the command is hidden from DMs
    # at the slash-command picker level. The guild_id None guard inside
    # the handler is the defense-in-depth layer for misconfigurations.
    contexts=[hikari.ApplicationContextType.GUILD],
):
    url = lightbulb.string(
        "url",
        "YouTube video or playlist URL.",
        min_length=1,
        max_length=500,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.defer()
        await _handle_play(ctx, self.url)


async def _handle_play(ctx: lightbulb.Context, url: str) -> None:
    """Run the full /play flow. Errors are caught and rendered ephemerally.

    Pulled out of the command class so tests can drive the same logic
    without going through lightbulb's invocation machinery.
    """
    guild_id = ctx.guild_id
    if guild_id is None:
        # ``contexts=[GUILD]`` should already prevent this, but if a
        # future re-registration drops the constraint we fail safe.
        await ctx.respond("Run /play in a server.", ephemeral=True)
        return

    if not is_supported_url(url):
        await ctx.respond("Only YouTube URLs are supported.", ephemeral=True)
        return

    cache = audio_cache.get_audio_cache()
    ll_client = lavalink_glue.get_lavalink_client()
    # Empty ``available_nodes`` distinguishes "lavalink client constructed
    # but socket isn't open yet" (early boot, server restart) from a fully
    # missing client. Both surface as the same friendly message — users
    # don't care which layer is asleep.
    if cache is None or ll_client is None or not ll_client.node_manager.available_nodes:
        await ctx.respond(
            "Audio service is down. Try again in a minute.",
            ephemeral=True,
        )
        return

    bot = cast(hikari.GatewayBot, ctx.client.app)
    voice_check = _check_voice_state(bot, guild_id, ctx.user.id)
    if isinstance(voice_check, str):
        await ctx.respond(voice_check, ephemeral=True)
        return
    user_channel_id = voice_check

    # Track the channel before the long async work below so a
    # TrackException firing mid-load posts back to the right place.
    lavalink_glue.last_play_channel[guild_id] = int(ctx.channel_id)

    if _is_playlist_url(url):
        await _play_playlist(ctx, url, cache, ll_client, bot, guild_id, user_channel_id)
    else:
        await _play_single(ctx, url, cache, ll_client, bot, guild_id, user_channel_id)


def _check_voice_state(bot: hikari.GatewayBot, guild_id: int, user_id: int) -> int | str:
    """Return the user's voice channel id, or an ephemeral error string.

    Encodes all the precondition checks that don't depend on the
    yt-dlp resolution: user must be in voice, the channel must be
    non-stage, and the bot (if already in voice) must be in the
    same channel.
    """
    user_state = bot.cache.get_voice_state(guild_id, user_id)
    if user_state is None or user_state.channel_id is None:
        return "Join a voice channel first."

    user_channel_id = int(user_state.channel_id)
    user_channel = bot.cache.get_guild_channel(user_channel_id)
    # ``get_guild_channel`` returns ``None`` if the channel isn't in
    # the cache (e.g. recently created). Treating an unknown channel
    # type as "not stage" preserves the old behaviour rather than
    # blocking legitimate /play attempts on cold caches.
    if user_channel is not None and user_channel.type == hikari.ChannelType.GUILD_STAGE:
        return "Stage channels aren't supported. Use a regular voice channel."

    me = bot.get_me()
    if me is not None:
        bot_state = bot.cache.get_voice_state(guild_id, me.id)
        if (
            bot_state is not None
            and bot_state.channel_id is not None
            and int(bot_state.channel_id) != user_channel_id
        ):
            return f"I'm already playing in <#{int(bot_state.channel_id)}>. Join that channel."
    return user_channel_id


def _is_playlist_url(url: str) -> bool:
    """Detect whether ``url`` carries a YouTube playlist id (``list=`` query)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return "list" in parse_qs(parsed.query)


async def _connect_and_wait(
    bot: hikari.GatewayBot,
    guild_id: int,
    channel_id: int,
) -> bool:
    """Drive ``update_voice_state`` and block on the lavalink handshake.

    Returns ``False`` on handshake timeout — the caller maps that to a
    friendly ephemeral.
    """
    await bot.update_voice_state(guild_id, channel_id, self_deaf=True)
    return await lavalink_glue.wait_for_voice_ready(guild_id, timeout=_VOICE_READY_TIMEOUT_S)


async def _load_one(
    cache: audio_cache.AudioCache,
    ll_client: lavalink.Client,
    track_info: ytdlp.TrackInfo,
) -> lavalink.AudioTrack | None:
    """Download ``track_info`` and ask Lavalink for its AudioTrack.

    Returns ``None`` on any failure (yt-dlp error, lavalink LOAD_FAILED,
    lavalink EMPTY). Releases the audio cache pin on lavalink failure
    so a transient load problem cannot permanently block eviction
    (M1 §4 release contract).
    """
    try:
        path = await cache.get_or_download(track_info)
    except (FetchFailed, InvalidVideoID) as exc:
        _log.warning("yt-dlp download failed for %s: %s", track_info.url, exc)
        return None

    nodes = list(ll_client.node_manager.nodes)
    if not nodes:
        await cache.release(track_info.video_id)
        _log.warning("no lavalink nodes available; dropping %s", track_info.url)
        return None

    try:
        result = await nodes[0].get_tracks(str(path))
    except Exception:
        await cache.release(track_info.video_id)
        _log.exception("lavalink get_tracks failed for %s", path)
        return None

    if result.load_type == lavalink.server.LoadType.ERROR:
        await cache.release(track_info.video_id)
        _log.warning(
            "lavalink load error for %s: %s",
            path,
            result.error if result.error else "unknown",
        )
        return None
    if not result.tracks:
        await cache.release(track_info.video_id)
        _log.warning("lavalink returned no tracks for %s", path)
        return None
    track = result.tracks[0]
    if isinstance(track, lavalink.DeferredAudioTrack):
        # ``LocalAudioSourceManager`` shouldn't ever surface deferred
        # tracks, but the union return type covers it; the Lavalink
        # encoded payload is what player.add ultimately needs.
        await cache.release(track_info.video_id)
        _log.warning("lavalink unexpectedly returned a DeferredAudioTrack for %s", path)
        return None
    return track


async def _play_single(
    ctx: lightbulb.Context,
    url: str,
    cache: audio_cache.AudioCache,
    ll_client: lavalink.Client,
    bot: hikari.GatewayBot,
    guild_id: int,
    channel_id: int,
) -> None:
    """Resolve + enqueue a single track URL."""
    try:
        track_info = await ytdlp.resolve_track(url, cache_root=cache.cache_root)
    except FetchFailed as exc:
        await ctx.respond(_friendly_message(exc), ephemeral=True)
        return

    player = ll_client.player_manager.create(guild_id=guild_id)
    if len(player.queue) + 1 > _QUEUE_CAP:
        await ctx.respond(
            f"Queue is full ({len(player.queue)}/{_QUEUE_CAP}). Wait for some tracks to finish.",
            ephemeral=True,
        )
        return

    if not await _connect_and_wait(bot, guild_id, channel_id):
        await ctx.respond(
            "Couldn't connect to the voice channel. Try again in a moment.",
            ephemeral=True,
        )
        return

    audio_track = await _load_one(cache, ll_client, track_info)
    if audio_track is None:
        await ctx.respond(
            "Could not load that track. Try a different URL.",
            ephemeral=True,
        )
        return

    was_playing = player.is_playing
    queue_len_before = len(player.queue)
    player.add(track=audio_track, requester=ctx.user.id)
    if not was_playing:
        await player.play()

    # ``position`` is 1-indexed; new track sits at queue_len_before + 1
    # when something was already playing, otherwise it's been pulled
    # out of the queue and is the current track ("playing now").
    embed = ux.build_queued_track_embed(
        track_info,
        position=queue_len_before + 1,
        playing_now=not was_playing,
    )
    await ctx.respond(embed=embed)


async def _play_playlist(
    ctx: lightbulb.Context,
    url: str,
    cache: audio_cache.AudioCache,
    ll_client: lavalink.Client,
    bot: hikari.GatewayBot,
    guild_id: int,
    channel_id: int,
) -> None:
    """Resolve playlist metadata, enqueue every track, then respond."""
    try:
        info, fetched_at, used_cache = await playlist_cache.fetch_with_fallback(
            url, cache_root=cache.cache_root
        )
    except FetchFailed as exc:
        await ctx.respond(_friendly_message(exc), ephemeral=True)
        return

    if not info.entries:
        await ctx.respond("That playlist is empty or private.", ephemeral=True)
        return

    player = ll_client.player_manager.create(guild_id=guild_id)
    incoming = len(info.entries)
    if len(player.queue) + incoming > _QUEUE_CAP:
        await ctx.respond(
            f"Queue is full ({len(player.queue)}/{_QUEUE_CAP}). Wait for some tracks to finish.",
            ephemeral=True,
        )
        return

    if not await _connect_and_wait(bot, guild_id, channel_id):
        await ctx.respond(
            "Couldn't connect to the voice channel. Try again in a moment.",
            ephemeral=True,
        )
        return

    was_playing = player.is_playing
    enqueued = 0
    for entry in info.entries:
        audio_track = await _load_one(cache, ll_client, entry)
        if audio_track is None:
            continue
        player.add(track=audio_track, requester=ctx.user.id)
        enqueued += 1
        if enqueued == 1 and not was_playing:
            # Start playback as soon as the first track is enqueued so
            # the user hears music while we keep loading the rest.
            await player.play()

    if enqueued == 0:
        await ctx.respond(
            "Could not load any tracks from that playlist.",
            ephemeral=True,
        )
        return

    cache_is_stale = used_cache and playlist_cache.is_stale(fetched_at)
    embed = ux.build_queued_playlist_embed(
        info,
        requester=ctx.user.username,
        used_cache=used_cache,
        fetched_at=fetched_at if used_cache else None,
        cache_is_stale=cache_is_stale,
        failed_count=incoming - enqueued,
    )
    await ctx.respond(embed=embed)


def _friendly_message(exc: FetchFailed) -> str:
    """Return the first arg of ``exc`` as a user-facing string.

    yt-dlp errors are pre-mapped to friendly sentences inside
    :mod:`ryzic.ytdlp` (M1 §6); we never re-translate them here.
    """
    if exc.args and isinstance(exc.args[0], str) and exc.args[0]:
        return exc.args[0]
    return "Could not load that URL."
