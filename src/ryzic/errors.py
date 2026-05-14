"""Domain exceptions for ryzic.

Centralized here so cross-module catches do not create import cycles.
"""

from __future__ import annotations

from typing import Any

from .i18n import t


class FetchFailed(Exception):
    """yt-dlp or audio-cache failed to load/resolve a URL.

    Carries a catalog ``key`` plus optional interpolation ``vars`` so the
    consumer (``commands.play._friendly_message``) renders the user-facing
    string at the caller's locale. ``args[0]`` is the en-US rendering so
    ``str(exc)`` / ``repr(exc)`` / log formatters still produce a readable
    line.
    """

    def __init__(self, key: str, /, **vars: Any) -> None:
        self.key = key
        self.vars = vars
        super().__init__(t(key, locale="en_US", **vars))


class InvalidVideoID(Exception):
    """A video ID failed character-set or path-safety validation."""
