"""Catalog-drift lint: holds ``t()`` call sites and ``en_US.json`` in sync.

Four checks, each runs as its own ``pytest`` row so a single offender names
itself in the failure summary:

1. **Missing-from-catalog**: every ``t("literal")`` / ``_broadcast_t("literal")``
   call site in ``src/ryzic/**`` resolves to a catalog key.
2. **Orphan-key**: every catalog key is referenced somewhere in ``src/``
   or ``tests/`` (any string-literal match counts — covers indirect refs
   like ``_FRIENDLY_ERROR_KEYS`` mapping values).
3. **Rails-plural shape**: every dict-valued catalog key uses Rails
   variant names (``zero``/``one``/``two``/``few``/``many``) and includes
   ``many`` (i18nice's catch-all). CLDR's ``other`` is rejected outright —
   i18nice silently returns the bare key for it.
4. **Variable-set**: every placeholder ``%{name}`` in a catalog template
   has a matching keyword argument at the call site. Catches typoed vars
   before they fire the ``_on_missing_placeholder`` ERROR log at runtime.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Iterator
from importlib import resources
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "ryzic"
_TESTS_ROOT = _REPO_ROOT / "tests"
_T_FUNCTIONS = frozenset({"t", "_broadcast_t"})
_RAILS_VARIANTS = frozenset({"zero", "one", "two", "few", "many"})
_NON_VAR_KWARGS = frozenset({"locale", "default"})
_PLACEHOLDER_RE = re.compile(r"%\{(\w+)\}")


def _load_catalog() -> dict[str, object]:
    """Load the en_US catalog body from the installed package data.

    Mirrors ``tests/test_imports.py``'s wheel-data canary so a packaging
    drift breaks here too rather than silently falling back to the source
    tree.
    """
    catalog_path = resources.files("ryzic.i18n.locales") / "en_US.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    return payload["en_US"]


def _walk_call_sites(roots: Iterable[Path]) -> Iterator[tuple[Path, ast.Call, str]]:
    """Yield ``(path, call_node, key_literal)`` for every ``t``/``_broadcast_t``
    call whose first positional arg is a string literal.

    Dynamic calls (``t(exc.key, ...)`` in ``commands/play.py``, ``t(key, ...)``
    in ``errors.py``) deliberately fall through — those keys are validated
    by the orphan-key string-literal scan instead.
    """
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    continue
                if name not in _T_FUNCTIONS or not node.args:
                    continue
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                yield path, node, first.value


def _string_literals(roots: Iterable[Path]) -> set[str]:
    """Return every string-constant value appearing in any ``*.py`` under ``roots``."""
    literals: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.add(node.value)
    return literals


def _template_placeholders(value: object) -> set[str]:
    """Return ``%{name}`` placeholder names in a catalog value (str or plural dict)."""
    if isinstance(value, str):
        return set(_PLACEHOLDER_RE.findall(value))
    if isinstance(value, dict):
        names: set[str] = set()
        for branch in value.values():
            if isinstance(branch, str):
                names.update(_PLACEHOLDER_RE.findall(branch))
        return names
    return set()


def test_every_t_call_site_resolves_to_a_catalog_key() -> None:
    """``t("foo.bar")`` with no ``foo.bar`` in the catalog is a guaranteed
    runtime regression: the helper logs at ERROR and returns the dotted
    key string to the user. Catch it at CI."""
    catalog = _load_catalog()
    missing: list[str] = []
    for path, node, key in _walk_call_sites([_SRC_ROOT]):
        if key not in catalog:
            rel = path.relative_to(_REPO_ROOT)
            missing.append(f"{rel}:{node.lineno} t({key!r}) — no such catalog key")
    assert not missing, "Missing catalog keys:\n  " + "\n  ".join(missing)


def test_every_catalog_key_has_a_caller() -> None:
    """Reverse direction: orphan keys after a PR cleanup are catalog rot.

    We accept any string-literal match in ``src/`` or ``tests/`` — that
    covers indirect references (``_FRIENDLY_ERROR_KEYS`` mapping values,
    test assertions that hard-code the key, etc.) without forcing every
    catalog key through a static ``t()`` call.
    """
    catalog = _load_catalog()
    refs = _string_literals([_SRC_ROOT, _TESTS_ROOT])
    orphans = sorted(set(catalog) - refs)
    assert not orphans, "Orphan catalog keys (no caller found):\n  " + "\n  ".join(orphans)


def test_plural_keys_use_rails_variants_with_many() -> None:
    """i18nice uses Rails ``many``, not CLDR ``other`` — the latter
    silently returns the bare key. Any plural-shaped catalog value must
    use only Rails variant names AND include ``many`` as the catch-all.
    """
    catalog = _load_catalog()
    problems: list[str] = []
    for key, value in catalog.items():
        if not isinstance(value, dict):
            continue
        variants = set(value.keys())
        unknown = variants - _RAILS_VARIANTS
        if unknown:
            problems.append(f"{key}: unknown plural variants {sorted(unknown)!r}")
        elif "many" not in variants:
            problems.append(f"{key}: plural key missing 'many' (Rails catch-all)")
    assert not problems, "Rails-plural lint:\n  " + "\n  ".join(problems)


def test_every_template_placeholder_is_passed_at_the_call_site() -> None:
    """Each ``%{name}`` in a catalog body must arrive as a ``name=...`` kwarg.

    Skips ``locale=`` and ``default=`` (consumed by ``t()`` itself, not
    interpolated into the template) and skips calls whose key is not in
    the catalog (already flagged by the missing-from-catalog test).
    """
    catalog = _load_catalog()
    problems: list[str] = []
    for path, node, key in _walk_call_sites([_SRC_ROOT]):
        if key not in catalog:
            continue
        expected = _template_placeholders(catalog[key])
        provided = {kw.arg for kw in node.keywords if kw.arg and kw.arg not in _NON_VAR_KWARGS}
        # ``**vars`` unpacking shows up as ``arg is None``; we can't verify
        # the names so we accept the call (e.g. ``commands/play.py:410``).
        if any(kw.arg is None for kw in node.keywords):
            continue
        missing = expected - provided
        if missing:
            rel = path.relative_to(_REPO_ROOT)
            problems.append(
                f"{rel}:{node.lineno} t({key!r}) missing kwargs {sorted(missing)!r} "
                f"(provided: {sorted(provided)!r})"
            )
    assert not problems, "Variable-set lint:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize(
    "key",
    sorted(k for k, v in _load_catalog().items() if isinstance(v, dict)),
)
def test_plural_keys_pass_count_at_every_call_site(key: str) -> None:
    """Defensive: every plural-shaped key needs ``count=...`` at the call
    site (i18nice's selector). Easy to forget; runtime symptom is silent
    fallback to the ``one`` branch."""
    misses: list[str] = []
    for path, node, k in _walk_call_sites([_SRC_ROOT]):
        if k != key:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "count" not in kwargs:
            rel = path.relative_to(_REPO_ROOT)
            misses.append(f"{rel}:{node.lineno} t({key!r}) missing count=")
    assert not misses, "Plural call without count:\n  " + "\n  ".join(misses)
