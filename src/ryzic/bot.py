"""Bot construction, lifecycle wiring, and process entrypoint."""

from __future__ import annotations

import logging

import dotenv
import hikari
import lightbulb

from . import config

_log = logging.getLogger(__name__)


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
    bot.subscribe(hikari.StartingEvent, client.start)
    bot.subscribe(hikari.StoppingEvent, client.stop)

    bot.run()


if __name__ == "__main__":
    main()
