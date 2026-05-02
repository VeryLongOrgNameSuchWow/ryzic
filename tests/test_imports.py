"""Smoke tests: package imports and trivial helpers behave."""

from __future__ import annotations

import pytest

from ryzic import bot, config, errors


def test_package_modules_import() -> None:
    assert hasattr(bot, "main")
    assert hasattr(config, "load")
    assert errors.FetchFailed.__mro__[1] is Exception
    assert errors.InvalidVideoID.__mro__[1] is Exception


def test_format_uptime_under_minute() -> None:
    assert bot._format_uptime(7) == "7s"


def test_format_uptime_minutes_seconds() -> None:
    assert bot._format_uptime(192) == "3m 12s"


def test_format_uptime_hours() -> None:
    assert bot._format_uptime(3_725) == "1h 2m 5s"


def test_format_uptime_days() -> None:
    assert bot._format_uptime(2 * 86_400 + 3_600 + 60 + 1) == "2d 1h 1m 1s"


def test_config_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="DISCORD_BOT_TOKEN"):
        config.load()


def test_config_parses_guild_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("RYZIC_GUILD_IDS", "111, 222 ,333")
    cfg = config.load()
    assert cfg.guild_ids == (111, 222, 333)


def test_config_rejects_bad_guild_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("RYZIC_GUILD_IDS", "111,oops")
    with pytest.raises(config.ConfigError, match="RYZIC_GUILD_IDS"):
        config.load()


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    for var in (
        "LAVALINK_HOST",
        "LAVALINK_PORT",
        "LAVALINK_PASSWORD",
        "RYZIC_CACHE_DIR",
        "RYZIC_CACHE_MAX_GB",
        "RYZIC_LOG_LEVEL",
        "RYZIC_GUILD_IDS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = config.load()
    assert cfg.lavalink_host == "lavalink"
    assert cfg.lavalink_port == 2333
    assert cfg.cache_max_gb == 5
    assert cfg.log_level == "INFO"
    assert cfg.guild_ids == ()


def test_config_rejects_bad_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PORT", "not-a-number")
    with pytest.raises(config.ConfigError, match="LAVALINK_PORT"):
        config.load()
