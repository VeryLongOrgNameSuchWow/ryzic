"""Bot construction, lifecycle wiring, and process entrypoint."""

from __future__ import annotations

import logging

import dotenv
import hikari
import lightbulb

from . import audio_cache, config, lavalink_glue

_log = logging.getLogger(__name__)

# 1 GiB in bytes — keeps the LRU cap arithmetic in a single place so the
# eviction loop and the env-var docs read the same units.
_BYTES_PER_GB = 1024**3


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


async def _bootstrap_audio_cache(cfg: config.Config) -> audio_cache.AudioCache:
    """Open the audio cache + run the orphan sweep before the bot serves traffic.

    The sweep runs unconditionally on startup: a previous crash may
    have left partial files in ``tmp/`` and orphaned audio files
    whose sqlite rows never made it to disk. Cheap once per boot.
    """
    cache = audio_cache.AudioCache(cfg.cache_dir, max_bytes=cfg.cache_max_gb * _BYTES_PER_GB)
    await cache.open()
    deleted = await audio_cache.sweep_orphans(cfg.cache_dir)
    if deleted:
        _log.info("startup orphan sweep removed %d files", deleted)
    audio_cache.set_audio_cache(cache)
    return cache


def main() -> None:
    dotenv.load_dotenv()
    cfg = config.load()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    _log.info("ryzic starting; log level=%s", cfg.log_level)

    bot = hikari.GatewayBot(
        token=cfg.discord_bot_token,
        intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_VOICE_STATES,
    )
    client = _build_client(bot, cfg)
    lavalink_glue.register_listeners(bot, cfg)

    cache: audio_cache.AudioCache | None = None

    async def _on_starting(_: hikari.StartingEvent) -> None:
        nonlocal cache
        # Audio cache must be ready BEFORE /play runs — start it before
        # syncing the slash commands so a fast user invocation can
        # never race the bootstrap.
        cache = await _bootstrap_audio_cache(cfg)
        # Extensions register commands; commands are synced inside
        # ``client.start``, so load before starting.
        await client.load_extensions("ryzic.commands.lltest", "ryzic.commands.play")
        await client.start()

    async def _on_stopping(_: hikari.StoppingEvent) -> None:
        await client.stop()
        if cache is not None:
            audio_cache.set_audio_cache(None)
            await cache.close()

    bot.subscribe(hikari.StartingEvent, _on_starting)
    bot.subscribe(hikari.StoppingEvent, _on_stopping)

    bot.run()


if __name__ == "__main__":
    main()
