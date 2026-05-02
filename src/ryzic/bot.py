"""Bot construction, lifecycle wiring, and process entrypoint."""

from __future__ import annotations

import logging
import time

import dotenv
import hikari
import lightbulb

from . import config

_log = logging.getLogger(__name__)


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86_400)
    hours, s = divmod(s, 3_600)
    minutes, s = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _build_client(
    bot: hikari.GatewayBot, cfg: config.Config, started_at: float
) -> lightbulb.Client:
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
            uptime = _format_uptime(time.monotonic() - started_at)
            await ctx.respond(f"Pong (uptime: {uptime})")

    return client


def main() -> None:
    dotenv.load_dotenv()
    cfg = config.load()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    _log.info("ryzic starting; log level=%s", cfg.log_level)

    started_at = time.monotonic()

    bot = hikari.GatewayBot(
        token=cfg.discord_bot_token,
        intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_VOICE_STATES,
    )
    client = _build_client(bot, cfg, started_at)
    bot.subscribe(hikari.StartingEvent, client.start)
    bot.subscribe(hikari.StoppingEvent, client.stop)

    bot.run()


if __name__ == "__main__":
    main()
