"""Smoke tests: package imports, config validation, secret-safe repr."""

from __future__ import annotations

from pathlib import Path

import pytest

from ryzic import bot, config, errors


def test_package_modules_import() -> None:
    assert hasattr(bot, "main")
    assert hasattr(config, "load")
    assert errors.FetchFailed.__mro__[1] is Exception
    assert errors.InvalidVideoID.__mro__[1] is Exception


def test_config_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="DISCORD_BOT_TOKEN"):
        config.load()


def test_config_parses_guild_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("RYZIC_GUILD_IDS", "111, 222 ,333")
    cfg = config.load()
    assert cfg.guild_ids == (111, 222, 333)


def test_config_rejects_bad_guild_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("RYZIC_GUILD_IDS", "111,oops")
    with pytest.raises(config.ConfigError, match="RYZIC_GUILD_IDS"):
        config.load()


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    for var in (
        "LAVALINK_HOST",
        "LAVALINK_PORT",
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
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("LAVALINK_PORT", "not-a-number")
    with pytest.raises(config.ConfigError, match="LAVALINK_PORT"):
        config.load()


def test_config_rejects_zero_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("LAVALINK_PORT", "0")
    with pytest.raises(config.ConfigError, match="LAVALINK_PORT"):
        config.load()


def test_config_rejects_negative_cache_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("RYZIC_CACHE_MAX_GB", "-1")
    with pytest.raises(config.ConfigError, match="RYZIC_CACHE_MAX_GB"):
        config.load()


def test_config_rejects_unknown_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("LAVALINK_PASSWORD", "fake-pwd")
    monkeypatch.setenv("RYZIC_LOG_LEVEL", "VERBOSE")
    with pytest.raises(config.ConfigError, match="RYZIC_LOG_LEVEL"):
        config.load()


def test_config_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-bot-token-xyz")
    monkeypatch.setenv("LAVALINK_PASSWORD", "secret-lavalink-pwd-xyz")
    # ``youtube_cookies_path`` is also ``field(repr=False)`` — it points
    # at a file containing live YouTube session tokens, so any incidental
    # ``repr(cfg)`` must stay clean of it (security review LOW-3).
    cookies = tmp_path / "secret-cookies-marker-xyz.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", str(cookies))
    cfg = config.load()
    rendered = repr(cfg)
    assert "secret-bot-token-xyz" not in rendered
    assert "secret-lavalink-pwd-xyz" not in rendered
    assert "secret-cookies-marker-xyz" not in rendered
