"""``/play`` slash command (M1 §3).

Single source of truth for all the URL → playback wiring. Friendly
error sentences come VERBATIM from the wrapper exceptions (M1 §6
fixed those to be user-presentable); we never re-translate them.

Order of operations is deliberately load-then-connect: voice is only
joined once we have a playable track. Otherwise a yt-dlp / Lavalink
load failure leaves the bot squatting in voice with no auto-leave
arming (only ``QueueEndEvent`` arms it, and nothing ever played).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import hikari
import lavalink
import lightbulb

from .. import audio_cache, lavalink_glue, playlist_cache, ux, ytdlp
from ..errors import FetchFailed, InvalidVideoID
from ..i18n import t
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

# M1 §3: a single user-facing string covers every "the audio plumbing
# isn't ready" failure (missing cache, missing lavalink client, no
# available nodes, voice handshake timeout). Users don't distinguish
# the layers — and a single string keeps the failure surface small.
_AUDIO_SERVICE_DOWN: str = "Audio service is down. Try again in a minute."


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
        await ctx.respond(
            t("voice.error.run_in_server", locale="en_US", command="play"),
            ephemeral=True,
        )
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
        await ctx.respond(_AUDIO_SERVICE_DOWN, ephemeral=True)
        return

    bot = cast(hikari.GatewayBot, ctx.client.app)
    user_state = bot.cache.get_voice_state(guild_id, ctx.user.id)
    if user_state is None or user_state.channel_id is None:
        await ctx.respond("Join a voice channel first.", ephemeral=True)
        return
    user_channel_id = int(user_state.channel_id)

    user_channel = bot.cache.get_guild_channel(user_channel_id)
    # ``get_guild_channel`` returns ``None`` if the channel isn't in
    # the cache (e.g. recently created). Treating an unknown channel
    # type as "not stage" preserves the old behaviour rather than
    # blocking legitimate /play attempts on cold caches.
    if user_channel is not None and user_channel.type == hikari.ChannelType.GUILD_STAGE:
        await ctx.respond(
            "Stage channels aren't supported. Use a regular voice channel.",
            ephemeral=True,
        )
        return

    me = bot.get_me()
    if me is not None:
        bot_state = bot.cache.get_voice_state(guild_id, me.id)
        if (
            bot_state is not None
            and bot_state.channel_id is not None
            and int(bot_state.channel_id) != user_channel_id
        ):
            await ctx.respond(
                f"I'm already playing in <#{int(bot_state.channel_id)}>. Join that channel.",
                ephemeral=True,
            )
            return

    # Track the channel before the long async work below so a
    # TrackException firing mid-load posts back to the right place.
    lavalink_glue.last_play_channel[guild_id] = int(ctx.channel_id)

    if _is_playlist_url(url):
        await _play_playlist(ctx, url, cache, ll_client, bot, guild_id, user_channel_id)
    else:
        await _play_single(ctx, url, cache, ll_client, bot, guild_id, user_channel_id)


def _is_playlist_url(url: str) -> bool:
    """Detect whether ``url`` carries a YouTube playlist id (``list=`` query)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return "list" in parse_qs(parsed.query)


async def _load_one(
    cache: audio_cache.AudioCache,
    ll_client: lavalink.Client,
    track_info: ytdlp.TrackInfo,
    *,
    cached_path: Path | None = None,
) -> lavalink.AudioTrack | None:
    """Download ``track_info`` and ask Lavalink for its AudioTrack.

    When ``cached_path`` is provided, ``track_info`` is already pinned by
    a prior :meth:`AudioCache.try_hit`; skip ``get_or_download``. Returns
    ``None`` on any failure (yt-dlp error, lavalink LOAD_FAILED, lavalink
    EMPTY). Releases the audio cache pin on lavalink failure so a
    transient load problem cannot permanently block eviction (M1 §4
    release contract).
    """
    if cached_path is not None:
        path = cached_path
    else:
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
    audio_track = result.tracks[0]
    # Lavalink can't read titles from bare-codec audio files; override (#136).
    audio_track.title = track_info.title
    audio_track.author = track_info.uploader
    return audio_track


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
    # Cache-first: a successful prior play of the same video survives
    # yt-dlp / YouTube breakage. Issue #132.
    track_info: ytdlp.TrackInfo | None = None
    cached_path: Path | None = None
    video_id = ytdlp.parse_video_id(url)
    if video_id is not None:
        hit = await cache.try_hit(video_id)
        if hit is not None:
            track_info = hit.track_info
            cached_path = hit.path

    if track_info is None:
        try:
            track_info = await ytdlp.resolve_track(url, cache_root=cache.cache_root)
        except FetchFailed as exc:
            await ctx.respond(_friendly_message(exc), ephemeral=True)
            return

    player = ll_client.player_manager.create(guild_id=guild_id)
    if len(player.queue) + 1 > _QUEUE_CAP:
        if cached_path is not None:
            await cache.release(track_info.video_id)
        await ctx.respond(
            f"Queue is full ({len(player.queue)}/{_QUEUE_CAP}). Wait for some tracks to finish.",
            ephemeral=True,
        )
        return

    # Load BEFORE connecting to voice — otherwise a load failure leaves
    # the bot dangling in voice with no auto-leave (the timer only arms
    # on QueueEndEvent, which needs a successful play).
    audio_track = await _load_one(cache, ll_client, track_info, cached_path=cached_path)
    if audio_track is None:
        await ctx.respond(
            "Could not load that track. Try a different URL.",
            ephemeral=True,
        )
        return

    await bot.update_voice_state(guild_id, channel_id, self_deaf=True)
    if not await lavalink_glue.wait_for_voice_ready(guild_id, timeout=_VOICE_READY_TIMEOUT_S):
        await cache.release(track_info.video_id)
        await ctx.respond(_AUDIO_SERVICE_DOWN, ephemeral=True)
        return

    was_playing = player.is_playing
    queue_len_before = len(player.queue)
    ux.attach_track_info(audio_track, track_info)
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
        channel_id=channel_id,
        requester_id=ctx.user.id,
        locale="en_US",
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

    # Load the first track BEFORE connecting to voice — otherwise a
    # playlist where every entry fails leaves the bot dangling in voice
    # (auto-leave only arms on QueueEndEvent, never reached here).
    # Search forward for the first loadable entry to keep the friendly
    # error semantically equivalent to the post-loop "all-fail" path.
    first_index = -1
    first_audio_track: lavalink.AudioTrack | None = None
    for i, entry in enumerate(info.entries):
        first_audio_track = await _load_one(cache, ll_client, entry)
        if first_audio_track is not None:
            first_index = i
            break

    if first_audio_track is None:
        await ctx.respond(
            "Could not load any tracks from that playlist.",
            ephemeral=True,
        )
        return

    await bot.update_voice_state(guild_id, channel_id, self_deaf=True)
    if not await lavalink_glue.wait_for_voice_ready(guild_id, timeout=_VOICE_READY_TIMEOUT_S):
        await cache.release(info.entries[first_index].video_id)
        await ctx.respond(_AUDIO_SERVICE_DOWN, ephemeral=True)
        return

    was_playing = player.is_playing
    ux.attach_track_info(first_audio_track, info.entries[first_index])
    player.add(track=first_audio_track, requester=ctx.user.id)
    if not was_playing:
        await player.play()
    enqueued = 1

    for entry in info.entries[first_index + 1 :]:
        audio_track = await _load_one(cache, ll_client, entry)
        if audio_track is None:
            continue
        ux.attach_track_info(audio_track, entry)
        player.add(track=audio_track, requester=ctx.user.id)
        enqueued += 1

    cache_is_stale = used_cache and playlist_cache.is_stale(fetched_at)
    embed = ux.build_queued_playlist_embed(
        info,
        requester=ctx.user.username,
        used_cache=used_cache,
        fetched_at=fetched_at if used_cache else None,
        cache_is_stale=cache_is_stale,
        failed_count=incoming - enqueued,
        locale="en_US",
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
