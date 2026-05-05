"""Runtime configuration loaded once from environment variables.

Validation happens at startup so a missing or malformed value fails the
process immediately rather than during a slash-command handler.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


# Pydantic-settings' default CSV decoder doesn't strip whitespace, but
# the legacy ``RYZIC_GUILD_IDS`` format tolerated ``"111, 222 ,333"``
# and empty pieces. Split here so the int-tuple coercion downstream sees
# clean string pieces (and reports per-piece errors normally).
def _split_guild_ids(raw: object) -> object:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        return tuple(piece.strip() for piece in raw.split(",") if piece.strip())
    return raw


# Empty string for an optional path is equivalent to "unset" — matches
# the project convention so an operator who comments a value with the
# trailing ``=`` left dangling still gets the default.
def _empty_to_none(raw: object) -> object:
    if raw == "":
        return None
    return raw


# Empty string for an optional non-negative-int defaults the field
# rather than tripping the int parser.
def _empty_to_default_300(raw: object) -> object:
    if raw is None or raw == "":
        return 300
    return raw


class Config(BaseSettings):
    """Validated environment-variable container.

    Each field declares ``validation_alias=AliasChoices("ENV_NAME", "field_name")``
    so the env source matches by env-var name AND tests can construct
    ``Config(field_name=...)`` by python field name. Plain ``alias=``
    plus ``populate_by_name=True`` would work at runtime but isn't
    visible to ty's static analysis of pydantic's generated ``__init__``.
    """

    # ``case_sensitive=True`` keeps env-var matching predictable: pydantic-settings
    # otherwise lowercases the lookup key, which would silently accept
    # ``discord_bot_token=...`` shells alongside the documented uppercase form
    # and complicate the threat model around env-var spoofing.
    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    # Secrets use ``repr=False`` so any incidental ``repr(cfg)`` (debug
    # log, config-dump command, error context) keeps the value out of
    # process logs. Plain ``str`` for downstream consumers (hikari /
    # lavalink clients) that don't speak ``SecretStr``.
    discord_bot_token: str = Field(
        validation_alias=AliasChoices("DISCORD_BOT_TOKEN", "discord_bot_token"),
        repr=False,
    )
    lavalink_host: str = Field(
        default="lavalink",
        validation_alias=AliasChoices("LAVALINK_HOST", "lavalink_host"),
    )
    lavalink_port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=2333,
        validation_alias=AliasChoices("LAVALINK_PORT", "lavalink_port"),
    )
    lavalink_password: str = Field(
        validation_alias=AliasChoices("LAVALINK_PASSWORD", "lavalink_password"),
        repr=False,
    )
    cache_dir: Path = Field(
        default=Path("./.cache"),
        validation_alias=AliasChoices("RYZIC_CACHE_DIR", "cache_dir"),
    )
    cache_max_gb: Annotated[int, Field(ge=1)] = Field(
        default=5,
        validation_alias=AliasChoices("RYZIC_CACHE_MAX_GB", "cache_max_gb"),
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        validation_alias=AliasChoices("RYZIC_LOG_LEVEL", "log_level"),
    )
    # ``NoDecode`` opts out of pydantic-settings' default JSON-list parsing
    # for complex types — the legacy CSV format (``"111, 222, 333"``) is
    # not valid JSON, so the env-source decoder is bypassed and the raw
    # string flows into the ``BeforeValidator`` above.
    guild_ids: Annotated[tuple[int, ...], NoDecode, BeforeValidator(_split_guild_ids)] = Field(
        default=(),
        validation_alias=AliasChoices("RYZIC_GUILD_IDS", "guild_ids"),
    )
    # ``0`` is a meaningful sentinel ("never auto-leave") so the lower
    # bound is ``ge=0`` rather than ``ge=1``.
    auto_leave_seconds: Annotated[int, Field(ge=0), BeforeValidator(_empty_to_default_300)] = Field(
        default=300,
        validation_alias=AliasChoices("RYZIC_AUTOLEAVE_SECONDS", "auto_leave_seconds"),
    )
    # ``repr=False`` matches the secret fields above: the path itself
    # is operator-controlled and not a credential, but it points at
    # a file containing live YouTube session tokens, so any incidental
    # ``repr(cfg)`` is kept from leaking it.
    youtube_cookies_path: Annotated[Path | None, BeforeValidator(_empty_to_none)] = Field(
        default=None,
        validation_alias=AliasChoices("RYZIC_YOUTUBE_COOKIES_PATH", "youtube_cookies_path"),
        repr=False,
    )

    @field_validator("discord_bot_token", "lavalink_password", mode="after")
    @classmethod
    def _reject_empty_secret(cls, value: str) -> str:
        # Empty string in the env preserves the legacy ``_require``
        # semantics of "unset → fail fast"; pydantic would otherwise
        # accept ``""`` as a valid string and silently start with a
        # blank token / password.
        if not value:
            raise ValueError("must be set to a non-empty value")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, raw: object) -> object:
        # The Literal constraint is uppercase-only; operators have always
        # been allowed to write ``debug`` / ``Info`` in their ``.env``.
        if isinstance(raw, str) and raw:
            return raw.upper()
        return raw


def _format_validation_error(exc: ValidationError) -> str:
    """Render a :class:`ValidationError` so the env-var name is visible.

    Tests assert ``pytest.raises(ConfigError, match="<ENV_VAR_NAME>")``,
    so the converted message must surface the offending env var.
    Pydantic puts the matched alias in ``loc[0]`` for both populated
    and missing-required errors — ``AliasChoices`` lists the env-var
    name first, so ``loc[0]`` is exactly the operator-facing name.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        env_name = str(loc[0]) if loc else "configuration"
        msg = err.get("msg") or "invalid value"
        if env_name in msg:
            parts.append(msg)
        else:
            parts.append(f"{env_name}: {msg}")
    return "; ".join(parts) if parts else str(exc)


def _check_cookies_path(path: Path | None) -> None:
    """Reject a ``RYZIC_YOUTUBE_COOKIES_PATH`` that the bot can't read.

    Threat model #6 / security review §2: a typo'd cookies path used to
    silently no-op (yt-dlp loads cookies via ``os.access(R_OK)`` and
    skips if the file is missing). Fail fast at startup instead.

    Lives in :func:`load` rather than as a ``field_validator`` so unit
    tests that build a :class:`Config` directly with a synthetic path
    aren't forced to materialise the file on disk first.
    """
    if path is None:
        return
    if not path.is_file():
        raise ConfigError(
            f"RYZIC_YOUTUBE_COOKIES_PATH points at a path that does not exist or "
            f"is not a regular file: {str(path)!r}. "
            f"Check the path is correct and that the bot user can read it."
        )
    if not os.access(path, os.R_OK):
        raise ConfigError(
            f"RYZIC_YOUTUBE_COOKIES_PATH points at a path the bot user cannot read: "
            f"{str(path)!r}. "
            f"Check the file's permissions (chmod 0o600 owned by the bot UID is typical)."
        )


def load() -> Config:
    """Build :class:`Config` from the current process environment.

    Wraps pydantic's :class:`ValidationError` in :class:`ConfigError`
    so callers (and tests) only need to catch a single project-local
    exception type and so the error string surfaces the offending
    env-var name rather than the python field name.
    """
    try:
        # ``BaseSettings.__init__`` accepts no positional/keyword args and
        # loads every required field from the env source — but ty reads
        # the signature pydantic generates from the model fields and
        # flags the secret fields as missing. Suppress just that.
        cfg = Config()  # ty: ignore[missing-argument]
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    _check_cookies_path(cfg.youtube_cookies_path)
    return cfg
