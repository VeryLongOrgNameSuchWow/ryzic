"""Bot construction, lifecycle wiring, and process entrypoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import dotenv
import hikari
import lightbulb

from . import config, lavalink_glue

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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


def _make_starter(
    client: lightbulb.Client,
) -> Callable[[hikari.StartingEvent], Coroutine[None, None, None]]:
    async def _on_starting(_: hikari.StartingEvent) -> None:
        # Extensions register commands; commands are synced inside
        # ``client.start``, so load before starting.
        await client.load_extensions("ryzic.commands.lltest")
        await client.start()

    return _on_starting


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
    lavalink_glue.register_di(client)
    lavalink_glue.register_listeners(bot, cfg)

    bot.subscribe(hikari.StartingEvent, _make_starter(client))
    bot.subscribe(hikari.StoppingEvent, client.stop)

    bot.run()


if __name__ == "__main__":
    main()
