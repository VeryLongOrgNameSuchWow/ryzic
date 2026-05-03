"""Bot startup-helper tests.

Currently focused on :func:`ryzic.bot._install_youtube_cookies`, which
mediates the opt-in YouTube cookies path. yt-dlp's ``YoutubeDL.__exit__``
calls ``save_cookies()`` unconditionally when ``cookiefile`` is set,
which opens the path in write mode — so the README's recommended ``:ro``
mount would raise ``PermissionError`` on every successful extraction
without the writable scratch copy this helper produces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ryzic import bot, config, ytdlp


def _make_cfg(cache_dir: Path, cookies_path: Path | None) -> config.Config:
    """Build a minimal :class:`config.Config` for the helper under test.

    Only the two fields the helper reads (``cache_dir`` and
    ``youtube_cookies_path``) need to be meaningful; the rest get
    placeholder values that satisfy the dataclass.
    """
    return config.Config(
        discord_bot_token="x",
        lavalink_host="lavalink",
        lavalink_port=2333,
        lavalink_password="x",
        cache_dir=cache_dir,
        cache_max_gb=5,
        log_level="INFO",
        guild_ids=(),
        youtube_cookies_path=cookies_path,
    )


@pytest.fixture(autouse=True)
def _restore_cookies_path() -> Any:
    """Mirror the fixture in test_ytdlp.py: keep the singleton clean."""
    original = ytdlp._COOKIES_PATH
    yield
    ytdlp.set_cookies_path(original)


def test_install_cookies_unset_clears_setter(tmp_path: Path) -> None:
    # Pre-pollute so we can prove the helper actively cleared it.
    ytdlp.set_cookies_path(tmp_path / "stale.txt")
    cfg = _make_cfg(cache_dir=tmp_path / "cache", cookies_path=None)
    bot._install_youtube_cookies(cfg)
    assert ytdlp._COOKIES_PATH is None


def test_install_cookies_unset_does_not_create_cache_dir(tmp_path: Path) -> None:
    # When cookies are off there's no reason to touch cache_dir from this
    # helper — the audio-cache bootstrap owns that path.
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=None)
    bot._install_youtube_cookies(cfg)
    assert not cache_dir.exists()


def test_install_cookies_passes_scratch_path_not_source(tmp_path: Path) -> None:
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\nsome=value\n")
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    with patch.object(ytdlp, "set_cookies_path") as set_path:
        bot._install_youtube_cookies(cfg)
    assert set_path.call_count == 1
    installed = set_path.call_args.args[0]
    # The scratch lives inside cache_dir, not at the operator's source.
    assert installed != source
    assert installed == cache_dir / bot._COOKIES_SCRATCH_FILENAME


def test_install_cookies_scratch_copy_matches_source_content(tmp_path: Path) -> None:
    source = tmp_path / "source-cookies.txt"
    expected = "# Netscape HTTP Cookie File\nsome=value\nother=token\n"
    source.write_text(expected)
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    bot._install_youtube_cookies(cfg)
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    assert scratch.read_text() == expected


def test_install_cookies_scratch_copy_is_mode_0o600(tmp_path: Path) -> None:
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    # Source has wide-open perms — the scratch must still land at 0o600
    # regardless of how loose the operator left the source.
    source.chmod(0o644)
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    bot._install_youtube_cookies(cfg)
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    # Mask off file-type bits; only the permission triplet matters.
    assert scratch.stat().st_mode & 0o777 == 0o600


def test_install_cookies_warning_references_source_path_not_scratch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The loud WARNING is the operator's recognition signal — it must
    # name the path they configured, not the internal scratch.
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    with caplog.at_level(logging.WARNING, logger="ryzic.bot"):
        bot._install_youtube_cookies(cfg)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(source) in r.getMessage() for r in warnings)
    scratch_filename = bot._COOKIES_SCRATCH_FILENAME
    # The warning must NOT mention the scratch path — that's the INFO line.
    assert not any(scratch_filename in r.getMessage() for r in warnings)


def test_install_cookies_info_logs_scratch_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    with caplog.at_level(logging.INFO, logger="ryzic.bot"):
        bot._install_youtube_cookies(cfg)
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(str(scratch) in r.getMessage() for r in info_records)


def test_install_cookies_rebuilds_scratch_on_each_call(tmp_path: Path) -> None:
    # Operators may rotate cookies between restarts. The helper must
    # re-copy fresh from source rather than reusing a stale scratch from
    # a prior run.
    source = tmp_path / "source-cookies.txt"
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)

    source.write_text("first-export\n")
    bot._install_youtube_cookies(cfg)
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    assert scratch.read_text() == "first-export\n"

    source.write_text("second-export\n")
    bot._install_youtube_cookies(cfg)
    assert scratch.read_text() == "second-export\n"


def test_install_cookies_does_not_modify_source(tmp_path: Path) -> None:
    # Simulate yt-dlp's session-refresh write hitting the scratch path:
    # the operator's source must remain byte-identical to what they wrote.
    source = tmp_path / "source-cookies.txt"
    pristine = "# Netscape HTTP Cookie File\noriginal=value\n"
    source.write_text(pristine)
    cache_dir = tmp_path / "cache"
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    bot._install_youtube_cookies(cfg)
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    # Stand in for ``YoutubeDL.__exit__ → save_cookies()``.
    scratch.write_text("rewritten-by-ytdlp\n")
    assert source.read_text() == pristine


def test_install_cookies_unset_unlinks_stale_scratch(tmp_path: Path) -> None:
    # Operator had cookies enabled in a previous run (scratch copy on disk),
    # then unset RYZIC_YOUTUBE_COOKIES_PATH and restarted. The helper must
    # remove the stale scratch so "env var unset" matches "no credential on
    # disk" — see security re-review on PR #51.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch = cache_dir / bot._COOKIES_SCRATCH_FILENAME
    scratch.write_text("# leftover from previous run\nsome=value\n")
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=None)
    bot._install_youtube_cookies(cfg)
    assert not scratch.exists()


def test_install_cookies_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    # First-startup ordering: ``_install_youtube_cookies`` runs before
    # ``_bootstrap_audio_cache``, so the cache_dir may not yet exist.
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    cache_dir = tmp_path / "fresh-cache"
    assert not cache_dir.exists()
    cfg = _make_cfg(cache_dir=cache_dir, cookies_path=source)
    bot._install_youtube_cookies(cfg)
    assert cache_dir.is_dir()
