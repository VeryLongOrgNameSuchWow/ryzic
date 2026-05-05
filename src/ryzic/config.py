"""Runtime configuration loaded once from environment variables.

Validation happens at startup so a missing or malformed value fails the
process immediately rather than during a slash-command handler.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    discord_bot_token: str = field(repr=False)
    lavalink_host: str
    lavalink_port: int
    lavalink_password: str = field(repr=False)
    cache_dir: Path
    cache_max_gb: int
    log_level: str
    guild_ids: tuple[int, ...]
    # Seconds to wait after ``QueueEndEvent`` before disconnecting from
    # voice. ``0`` disables the timer entirely (24/7 ambient music
    # deployments). Default ``300`` preserves the historical behaviour.
    auto_leave_seconds: int = 300
    # ``repr=False`` matches ``discord_bot_token`` / ``lavalink_password``:
    # the path itself is operator-controlled and not a credential, but it
    # points at a file containing live YouTube session tokens, so any
    # incidental ``repr(cfg)`` (debug log, config-dump command, error
    # context) is kept from leaking it.
    youtube_cookies_path: Path | None = field(repr=False, default=None)


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
    for piece in raw.split(","):
        chunk = piece.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"RYZIC_GUILD_IDS contains a non-integer value: {chunk!r}") from exc
    return tuple(ids)


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} must be >= 1, got {value}")
    return value


def _parse_non_negative_int(name: str, default: int) -> int:
    """Parse an int env var allowing ``0``.

    Distinct from :func:`_parse_positive_int` because ``RYZIC_AUTOLEAVE_SECONDS``
    treats ``0`` as a meaningful sentinel (disables the auto-leave timer)
    rather than a misconfiguration.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must be >= 0, got {value}")
    return value


def _parse_log_level(raw: str | None) -> str:
    candidate = (raw or "INFO").upper()
    if candidate not in logging.getLevelNamesMapping():
        raise ConfigError(
            f"RYZIC_LOG_LEVEL must be one of CRITICAL/ERROR/WARNING/INFO/DEBUG, got {candidate!r}"
        )
    return candidate


def _parse_optional_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return Path(raw)


def _parse_existing_file(name: str) -> Path | None:
    """Read an optional env var pointing at an existing readable file.

    Returns ``None`` when the env var is unset or empty (the operator
    didn't opt in). When set, the path is required to exist, be a
    regular file, and be readable by the bot's UID — otherwise we fail
    fast at startup with an actionable error rather than letting yt-dlp
    silently no-op a typo'd cookies path (security review §2 / threat
    model question #6).
    """
    path = _parse_optional_path(name)
    if path is None:
        return None
    raw = os.environ.get(name)
    if not path.is_file():
        raise ConfigError(
            f"{name} points at a path that does not exist or is not a regular file: {raw!r}. "
            f"Check the path is correct and that the bot user can read it."
        )
    if not os.access(path, os.R_OK):
        raise ConfigError(
            f"{name} points at a path the bot user cannot read: {raw!r}. "
            f"Check the file's permissions (chmod 0o600 owned by the bot UID is typical)."
        )
    return path


def load() -> Config:
    return Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        lavalink_host=os.environ.get("LAVALINK_HOST", "lavalink"),
        lavalink_port=_parse_positive_int("LAVALINK_PORT", 2333),
        lavalink_password=_require("LAVALINK_PASSWORD"),
        cache_dir=Path(os.environ.get("RYZIC_CACHE_DIR", "./.cache")),
        cache_max_gb=_parse_positive_int("RYZIC_CACHE_MAX_GB", 5),
        log_level=_parse_log_level(os.environ.get("RYZIC_LOG_LEVEL")),
        guild_ids=_parse_guild_ids(os.environ.get("RYZIC_GUILD_IDS")),
        auto_leave_seconds=_parse_non_negative_int("RYZIC_AUTOLEAVE_SECONDS", 300),
        youtube_cookies_path=_parse_existing_file("RYZIC_YOUTUBE_COOKIES_PATH"),
    )
