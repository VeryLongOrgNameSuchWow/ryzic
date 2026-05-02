"""URL validator tests (M1 §6 — HIGH-1 fix)."""

from __future__ import annotations

import pytest

from ryzic.url_validator import ALLOWED_HOSTS, is_supported_url


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
    ],
)
def test_each_allowed_host_accepted(url: str) -> None:
    assert is_supported_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # HIGH-1 regression: regex-based check would match this.
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://evil.com/youtube.com/watch?v=dQw4w9WgXcQ",
        "https://evilyoutube.com/watch?v=dQw4w9WgXcQ",
        # HTTP is a downgrade attack vector.
        "http://youtube.com/watch?v=dQw4w9WgXcQ",
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        # Other schemes.
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "javascript:alert(1)",
        "file:///etc/passwd",
        # Other video sites — out of scope for M1.
        "https://vimeo.com/123",
        "https://soundcloud.com/foo/bar",
    ],
)
def test_disallowed_urls_rejected(url: str) -> None:
    assert is_supported_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "://no-scheme",
        "https://",
        "https:///path",
    ],
)
def test_malformed_urls_rejected(url: str) -> None:
    assert is_supported_url(url) is False


def test_urlparse_value_error_is_swallowed() -> None:
    # ``urlparse`` raises ``ValueError`` for malformed IPv6 brackets;
    # treat it as a soft reject rather than crashing the command.
    assert is_supported_url("https://[::1") is False


def test_userinfo_in_url_does_not_smuggle_host() -> None:
    # URL parsers historically split on `@`; the validator must use the
    # post-userinfo hostname, not the raw netloc, or ``user@evil.com``
    # could masquerade as the allowlisted host.
    assert is_supported_url("https://youtube.com@evil.com/watch?v=x") is False


def test_hostname_comparison_is_case_insensitive_via_urlparse() -> None:
    # ``urlparse`` lowercases hostnames; the allowlist relies on that.
    assert is_supported_url("https://YouTube.com/watch?v=dQw4w9WgXcQ") is True


def test_allowed_hosts_is_immutable() -> None:
    assert isinstance(ALLOWED_HOSTS, frozenset)
