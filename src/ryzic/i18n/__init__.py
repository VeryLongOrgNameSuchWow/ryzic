"""i18nice-backed translation helper.

Catalog lives in ``ryzic/i18n/locales/{locale}.json``. ``en_US.json`` is the
only locale today; second-locale support is intentionally a future change
(plural rules + ``add_function`` hook line below are commented for now).

On configuration failure ``t()`` returns the raw key via ``i18nice``'s
``on_missing_translation`` hook — every embed shows a dotted key string,
but the bot still boots. Catalog-drift CI lint (planned, post-scaffold)
gates this before deploy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import i18n
import lightbulb

log = logging.getLogger(__name__)
_DEFAULT_LOCALE = "en_US"
_LOCALES_DIR = Path(__file__).resolve().parent / "locales"


def _on_missing_translation(key: str, locale: str, **_kwargs: Any) -> str:
    log.error("missing i18n key %r (locale=%s)", key, locale)
    return key


def _on_missing_placeholder(key: str, locale: str, _template: str, name: str) -> str:
    log.error("missing i18n placeholder %r in %r (locale=%s)", name, key, locale)
    return f"%{{{name}}}"


def _configure() -> None:
    try:
        # i18nice is a process-global singleton; ``load_path`` persists
        # across ``_configure()`` re-invocations (tests re-run it). Guard
        # the append so repeated calls don't accumulate duplicate entries.
        if str(_LOCALES_DIR) not in i18n.load_path:
            i18n.load_path.append(str(_LOCALES_DIR))
        i18n.set("filename_format", "{locale}.{format}")
        i18n.set("file_format", "json")
        i18n.set("fallback", _DEFAULT_LOCALE)
        i18n.set("on_missing_translation", _on_missing_translation)
        i18n.set("on_missing_placeholder", _on_missing_placeholder)
        # Polish hook lands with the second-locale wave:
        # i18n.add_function("pluralize", _pluralize_pl, locale="pl")
    except Exception:
        log.exception("i18n configuration failed; t() will return raw keys")


_configure()


def t(key: str, *, locale: str | None = None, **vars: Any) -> str:
    return i18n.t(key, locale=locale or _DEFAULT_LOCALE, **vars)


def locale_for_ephemeral(ctx: lightbulb.Context) -> str:
    return getattr(ctx.interaction, "locale", None) or _DEFAULT_LOCALE


def locale_for_public(ctx: lightbulb.Context) -> str:
    i = ctx.interaction
    return getattr(i, "guild_locale", None) or getattr(i, "locale", None) or _DEFAULT_LOCALE


def _broadcast_t(key: str, **vars: Any) -> str:
    return t(key, locale=_DEFAULT_LOCALE, **vars)
