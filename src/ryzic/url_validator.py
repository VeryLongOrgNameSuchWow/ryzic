"""YouTube URL allowlist (per M1 §6 — fixes review HIGH-1).

A regex-based check is rejected upstream because patterns like
``youtube\\.com`` happily match ``youtube.com.evil.com``. Use the stdlib
URL parser, then compare the **hostname** (not the netloc, which can
include ``user@host:port`` noise) against an explicit set.
"""

from __future__ import annotations

from urllib.parse import urlparse

ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)


def is_supported_url(url: str) -> bool:
    """Return True iff ``url`` is HTTPS and hosted on an allowlisted YouTube domain."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
