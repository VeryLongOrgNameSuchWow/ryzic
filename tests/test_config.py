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


def test_youtube_cookies_path_set_resolves_to_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    cookies = tmp_path / "youtube-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", str(cookies))
    cfg = config.load()
    assert cfg.youtube_cookies_path == cookies


def test_youtube_cookies_path_missing_file_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Threat model #6 / security review §2: a typo'd cookies path used to
    # silently no-op (yt-dlp loads cookies via os.access(R_OK) and skips
    # if the file is missing). Now it must fail fast at startup.
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", str(tmp_path / "does-not-exist.txt"))
    with pytest.raises(config.ConfigError, match="RYZIC_YOUTUBE_COOKIES_PATH"):
        config.load()


def test_youtube_cookies_path_directory_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``is_file()`` rejects directories — operators who point at a dir
    # (e.g. forgot the filename) get a clear error rather than a yt-dlp
    # IsADirectoryError on first /play.
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", str(tmp_path))
    with pytest.raises(config.ConfigError, match="RYZIC_YOUTUBE_COOKIES_PATH"):
        config.load()


def test_youtube_cookies_path_unreadable_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Permissions misconfiguration (file exists but bot UID can't read it)
    # gets the same fail-fast treatment so the operator sees the issue at
    # startup rather than via a confusing downstream rejection.
    _clean_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    cookies = tmp_path / "youtube-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    cookies.chmod(0o000)
    try:
        monkeypatch.setenv("RYZIC_YOUTUBE_COOKIES_PATH", str(cookies))
        with pytest.raises(config.ConfigError, match="RYZIC_YOUTUBE_COOKIES_PATH"):
            config.load()
    finally:
        # Restore so tmp_path cleanup doesn't fail under strict umasks.
        cookies.chmod(0o600)
