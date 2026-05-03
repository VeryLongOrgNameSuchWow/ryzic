"""Config loader tests.

Focused on env-var → :class:`Config` mapping. Uses ``monkeypatch`` so
each test runs against a clean environment slice without leaking into
sibling tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ryzic import config


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ryzic-relevant env var so defaults take over."""
    for name in (
        "DISCORD_BOT_TOKEN",
        "LAVALINK_HOST",
        "LAVALINK_PORT",
        "LAVALINK_PASSWORD",
        "RYZIC_CACHE_DIR",
        "RYZIC_CACHE_MAX_GB",
        "RYZIC_LOG_LEVEL",
        "RYZIC_GUILD_IDS",
        "RYZIC_YOUTUBE_COOKIES_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_youtube_cookies_path_unset_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    cfg = config.load()
    assert cfg.youtube_cookies_path is None


def test_youtube_cookies_path_empty_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty string is treated identically to unset — neither implies
    # the operator opted in. Mirrors the .env.example commenting style.
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", "")
    cfg = config.load()
    assert cfg.youtube_cookies_path is None


def test_youtube_cookies_path_set_resolves_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", "/etc/ryzic/youtube-cookies.txt")
    cfg = config.load()
    assert cfg.youtube_cookies_path == Path("/etc/ryzic/youtube-cookies.txt")
