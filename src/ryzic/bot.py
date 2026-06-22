"""Bot construction, lifecycle wiring, and process entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil

import dotenv
import hikari
import lightbulb

from . import audio_cache, config, lavalink_glue, now_playing, now_playing_buttons, ytdlp

_log = logging.getLogger(__name__)

# Filename of the writable scratch copy of the operator's cookies file
# inside ``cfg.cache_dir``. Kept as a module constant so tests can refer
# to the same name without hard-coding a literal in two places.
_COOKIES_SCRATCH_FILENAME = "youtube-cookies.txt"


def _build_client(bot: hikari.GatewayBot, cfg: config.Config) -> lightbulb.Client:
    client = lightbulb.client_from_app(
        bot,
        default_enabled_guilds=cfg.guild_ids,
    )

    @client.register
    class Ping(
        lightbulb.SlashCommand,
        name="ping",
        description="Check that the bot is alive.",
    ):
        @lightbulb.invoke
        async def invoke(self, ctx: lightbulb.Context) -> None:
            await ctx.respond("Pong")

    return client


def _install_youtube_cookies(cfg: config.Config) -> None:
    """Install the opt-in YouTube cookies file for yt-dlp, via a writable scratch copy.

    yt-dlp's ``YoutubeDL.__exit__`` unconditionally calls
    ``save_cookies()`` whenever ``cookiefile`` is set, which opens the
    path in write mode. The README's recommended ``:ro`` bind mount
    therefore raises ``PermissionError`` on every successful extraction,
    breaking every ``/play`` once cookies are enabled.

    To preserve the ``:ro`` source posture we copy the operator's file
    into the bot's private cache directory at startup and hand yt-dlp
    the scratch path. yt-dlp's session-refresh writes stay contained to
    a volume the bot already owns; the operator's source file is never
    modified. Refreshed session tokens are lost on restart, which is
    fine — browser-exported cookies expire anyway and operators
    periodically re-export.
    """
    if cfg.youtube_cookies_path is None:
        # Remove a scratch copy left by a previous run that had cookies enabled,
        # so unsetting the env var fully matches the operator's mental model
        # ("cookies disabled") rather than leaving a stale credential on disk.
        (cfg.cache_dir / _COOKIES_SCRATCH_FILENAME).unlink(missing_ok=True)
        ytdlp.set_cookies_path(None)
        return

    scratch_path = cfg.cache_dir / _COOKIES_SCRATCH_FILENAME
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    # ``copyfile`` overwrites by default — the scratch copy is rebuilt
    # fresh on every startup so a rotated source file takes effect on
    # restart rather than reusing a stale copy from a prior run.
    shutil.copyfile(cfg.youtube_cookies_path, scratch_path)
    # Restrict to bot UID; the parent cache_dir is already private but
    # mode the scratch file explicitly so it's never world/group readable
    # regardless of the operator's umask.
    scratch_path.chmod(0o600)

    # WARNING references the operator's source path — that's the path
    # they configured and recognize. The INFO follow-up notes the
    # internal scratch copy for transparency.
    _log.warning(
        "YouTube cookies enabled from %s — every /play-er can fetch any "
        "video this account can see. See README.",
        cfg.youtube_cookies_path,
    )
    _log.info(
        "YouTube cookies copied to writable scratch path %s "
        "(yt-dlp session refreshes are contained to the bot's cache volume)",
        scratch_path,
    )
    ytdlp.set_cookies_path(scratch_path)


async def _bootstrap_audio_cache(cfg: config.Config) -> audio_cache.AudioCache:
    """Open the audio cache + run the orphan sweep before the bot serves traffic.

    The sweep runs unconditionally on startup: a previous crash may
    have left partial files in ``tmp/`` and orphaned audio files
    whose sqlite rows never made it to disk. Cheap once per boot.
    """
    cache = audio_cache.AudioCache(cfg.cache_dir, max_bytes=cfg.cache_max_gb * 1024**3)
    await cache.open()
    deleted = await audio_cache.sweep_orphans(cfg.cache_dir)
    if deleted:
        _log.info("startup orphan sweep removed %d files", deleted)
    audio_cache.set_audio_cache(cache)
    return cache


async def _update_controllers_loop(bot: hikari.GatewayBot) -> None:
    """Periodically refresh the now-playing controllers to advance progress bars."""
    while True:
        try:
            await asyncio.sleep(15)
            await now_playing.refresh_all(bot)
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("periodic controller refresh loop failed")


def main() -> None:
    dotenv.load_dotenv()
    cfg = config.load()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    _log.info("ryzic starting; log level=%s", cfg.log_level)

    _install_youtube_cookies(cfg)

    bot = hikari.GatewayBot(
        token=cfg.discord_bot_token,
        intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_VOICE_STATES,
    )
    client = _build_client(bot, cfg)
    lavalink_glue.register_listeners(bot, cfg)
    now_playing_buttons.register_listener(bot)

    cache: audio_cache.AudioCache | None = None
    refresh_task: asyncio.Task[None] | None = None

    async def _on_starting(_: hikari.StartingEvent) -> None:
        nonlocal cache, refresh_task
        # Audio cache must be ready BEFORE /play runs — start it before
        # syncing the slash commands so a fast user invocation can
        # never race the bootstrap.
        cache = await _bootstrap_audio_cache(cfg)
        # Extensions register commands; commands are synced inside
        # ``client.start``, so load before starting.
        await client.load_extensions(
            "ryzic.commands.play",
            "ryzic.commands.skip",
            "ryzic.commands.queue",
            "ryzic.commands.pause",
            "ryzic.commands.resume",
            "ryzic.commands.leave",
            "ryzic.commands.seek",
            "ryzic.commands.recent",
            "ryzic.commands.replay",
            "ryzic.commands.np",
        )
        refresh_task = asyncio.create_task(_update_controllers_loop(bot))
        await client.start()

    async def _on_stopping(_: hikari.StoppingEvent) -> None:
        await client.stop()
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        if cache is not None:
            audio_cache.set_audio_cache(None)
            await cache.close()

    bot.subscribe(hikari.StartingEvent, _on_starting)
    bot.subscribe(hikari.StoppingEvent, _on_stopping)

    bot.run()


if __name__ == "__main__":
    main()
