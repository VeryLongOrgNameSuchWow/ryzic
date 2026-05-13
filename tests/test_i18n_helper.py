"""Tests for ``ryzic.i18n`` — i18nice-backed translation helper.

Covers the four hook surfaces the helper is responsible for:

- Smoke render via the sentinel ``i18n.smoke`` catalog key.
- Missing-key fires ``_on_missing_translation`` (logs at ERROR, returns key).
- Missing-var fires ``_on_missing_placeholder`` (logs at ERROR, returns
  literal ``%{name}``).
- Malformed-catalog fallback: simulate by re-running ``_configure()`` with
  a monkeypatched raise; ``t()`` still returns something usable (the key).
"""

from __future__ import annotations

import logging
from typing import cast

import lightbulb
import pytest

from ryzic import i18n as ryzic_i18n


def test_smoke_returns_sentinel_value() -> None:
    assert ryzic_i18n.t("i18n.smoke") == "ok"


def test_smoke_honors_explicit_locale_kwarg() -> None:
    assert ryzic_i18n.t("i18n.smoke", locale="en_US") == "ok"


def test_missing_key_returns_key_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=ryzic_i18n.log.name):
        rendered = ryzic_i18n.t("does.not.exist")
    assert rendered == "does.not.exist"
    assert any(
        "missing i18n key" in r.message and "does.not.exist" in r.message for r in caplog.records
    )


def test_missing_placeholder_leaves_literal_and_logs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Register a temporary key with a placeholder, call without providing it.
    import i18n

    i18n.add_translation("i18n._test.greet", "Hi %{name}", locale="en_US")
    try:
        with caplog.at_level(logging.ERROR, logger=ryzic_i18n.log.name):
            rendered = ryzic_i18n.t("i18n._test.greet")
        assert rendered == "Hi %{name}"
        assert any(
            "missing i18n placeholder" in r.message and "'name'" in r.message
            for r in caplog.records
        )
    finally:
        # Best-effort cleanup; translations.remove is internal so we just
        # overwrite with a no-op marker.
        i18n.add_translation("i18n._test.greet", "", locale="en_US")


def test_malformed_catalog_falls_back_to_returning_keys(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``_configure()`` raises, ``t()`` must still return something.

    We simulate the failure by monkeypatching ``i18n.set`` to raise, then
    re-running ``_configure()`` and asserting the exception was swallowed
    and logged. The sentinel key remains resolvable because the process-
    level i18nice state from import time is still loaded — what this test
    really proves is the try/except guard around ``_configure()`` itself
    doesn't crash the bot.
    """
    import i18n

    def boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated catalog parse failure")

    monkeypatch.setattr(i18n, "set", boom)
    with caplog.at_level(logging.ERROR, logger=ryzic_i18n.log.name):
        ryzic_i18n._configure()  # must not raise
    assert any("i18n configuration failed" in r.message for r in caplog.records)
    # ``t()`` still works — at minimum returns the key string for any input.
    assert ryzic_i18n.t("any.key.at.all") == "any.key.at.all"


def test_locale_resolvers_default_to_en_us() -> None:
    """Locale resolvers fall back to en_US when the interaction is bare.

    The helpers only do ``getattr`` with a default, so a duck-typed stub
    with no ``locale`` / ``guild_locale`` attributes is sufficient. We
    ``cast`` rather than building a real Context (slot-heavy, irrelevant).
    """

    class _BareInteraction:
        pass

    class _BareCtx:
        interaction = _BareInteraction()

    ctx = cast(lightbulb.Context, _BareCtx())
    assert ryzic_i18n.locale_for_ephemeral(ctx) == "en_US"
    assert ryzic_i18n.locale_for_public(ctx) == "en_US"


def test_locale_for_public_prefers_guild_locale() -> None:
    class _Interaction:
        locale = "fr"
        guild_locale = "de"

    class _Ctx:
        interaction = _Interaction()

    ctx = cast(lightbulb.Context, _Ctx())
    assert ryzic_i18n.locale_for_public(ctx) == "de"
    assert ryzic_i18n.locale_for_ephemeral(ctx) == "fr"


def test_broadcast_t_pins_default_locale() -> None:
    """``_broadcast_t`` must hard-code en_US regardless of process state."""
    assert ryzic_i18n._broadcast_t("i18n.smoke") == "ok"
