"""Runtime configuration loaded once from environment variables.

Validation happens at startup so a missing required var fails the process
immediately rather than during a slash-command handler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    lavalink_host: str
    lavalink_port: int
    lavalink_password: str
    cache_dir: Path
    cache_max_gb: int
    log_level: str
    guild_ids: tuple[int, ...]


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Required environment variable {name!r} is not set. "
            f"See .env.example for the full list."
        )
    return value


def _parse_guild_ids(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    ids: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"RYZIC_GUILD_IDS contains a non-integer value: {chunk!r}") from exc
    return tuple(ids)


def _parse_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load() -> Config:
    return Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        lavalink_host=os.environ.get("LAVALINK_HOST", "lavalink"),
        lavalink_port=_parse_int("LAVALINK_PORT", 2333),
        lavalink_password=os.environ.get("LAVALINK_PASSWORD", "youshallnotpass"),
        cache_dir=Path(os.environ.get("RYZIC_CACHE_DIR", "./.cache")),
        cache_max_gb=_parse_int("RYZIC_CACHE_MAX_GB", 5),
        log_level=os.environ.get("RYZIC_LOG_LEVEL", "INFO").upper(),
        guild_ids=_parse_guild_ids(os.environ.get("RYZIC_GUILD_IDS")),
    )
