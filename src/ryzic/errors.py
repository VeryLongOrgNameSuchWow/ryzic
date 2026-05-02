"""Domain exceptions for ryzic.

Centralized here so cross-module catches do not create import cycles.
"""

from __future__ import annotations


class FetchFailed(Exception):
    """yt-dlp or Lavalink failed to load/resolve a URL."""


class InvalidVideoID(Exception):
    """A video ID failed character-set or path-safety validation."""
