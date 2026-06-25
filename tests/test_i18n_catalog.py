"""Catalog-drift lint: holds render sites and ``en_US.json`` in sync.

Six checks, each runs as its own ``pytest`` row so a single offender names
itself in the failure summary:

1. **Missing-from-catalog**: every ``t("literal")`` / ``_broadcast_t("literal")``
   call site in ``src/ryzic/**`` resolves to a catalog key.
2. **Orphan-key**: every catalog key is referenced somewhere in ``src/``
   or ``tests/`` via a real string-literal usage (docstring-only references
   do NOT count — a docstring is not a render site, so it can't bless a
   dead key).
3. **Rails-plural shape**: every dict-valued catalog key uses Rails
   variant names (``zero``/``one``/``few``/``many``) and includes
   ``many`` (i18nice's catch-all). CLDR's ``other`` is rejected outright —
   i18nice silently returns the bare key for it. ``two`` is also rejected:
   i18nice's ``pluralize`` has no ``two`` codepath (``count == 2`` falls
   through to ``few``), so a ``two`` branch would be unreachable.
4. **Variable-set (t)**: every placeholder ``%{name}`` in a catalog template
   has a matching keyword argument at the ``t()``/``_broadcast_t()`` call
   site. Catches typoed vars before they fire the
   ``_on_missing_placeholder`` ERROR log at runtime.
5. **FetchFailed key**: every ``FetchFailed(...)`` construction resolves
   to a catalog key. Literal keys are checked directly; the one dynamic
   site (``ytdlp.py`` ``raise FetchFailed(key)``) is resolved via the
   ``_FRIENDLY_ERROR_KEYS`` dict literal. Any future unresolvable dynamic
   site fails the gate and demands an explicit allowlist entry + rationale.
6. **FetchFailed kwargs**: every placeholder in a literal-keyed
   ``FetchFailed`` template arrives as a kwarg at the raise site. The
   ``FetchFailed.__init__`` internal ``t(key, **vars)`` is NOT walked —
   it is covered transitively by the raise-site check (the raise site
   must pass every placeholder the template needs).

The catalog loader also rejects duplicate JSON keys at any depth (test
``test_catalog_has_no_duplicate_keys``); the runtime loader in
``src/ryzic/i18n/__init__.py`` intentionally logs-and-falls-back instead
of raising, so a typo doesn't block bot boot.

Gate logic is factored into pure ``_check_*`` helpers so the existing
gate tests run them against the real catalog AND meta-tests run them
against synthetic in-memory inputs (no temp files).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "ryzic"
_TESTS_ROOT = _REPO_ROOT / "tests"
# ``t``/``_broadcast_t`` -> normalized render kind. ``FetchFailed`` is matched
# separately (by Name OR Attribute) so a future qualified ``errors.FetchFailed``
# call site is also caught — there is no such site today.
_RENDER_KINDS = {"t": "t", "_broadcast_t": "broadcast_t"}
# i18nice's ``pluralize`` (translator.py) has branches for zero/one/few/many
# only; ``count == 2`` falls through to ``few``. A ``two`` branch would be
# unreachable dead code, so the lint hard-rejects it.
_RAILS_VARIANTS = frozenset({"zero", "one", "few", "many"})
_NON_VAR_KWARGS = frozenset({"locale", "default"})
_PLACEHOLDER_RE = re.compile(r"%\{(\w+)\}")

# Shared anchor name: the dynamic-site recognizer (see "Dynamic FetchFailed
# site recognition" section below) and the candidate resolver both key off
# this constant, so a rename of ``_FRIENDLY_ERROR_KEYS`` breaks both in
# lockstep (loud, pointing at the rename) rather than one path silently
# masking the other. The allowlist below is reserved for FUTURE dynamic
# sites whose key genuinely cannot be traced to a literal source — adding
# one requires an explicit entry here plus a rationale comment.
_FRIENDLY_ERROR_KEYS_NAME = "_FRIENDLY_ERROR_KEYS"
_DYNAMIC_FETCHFAILED_ALLOWLIST: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# Catalog loading
# --------------------------------------------------------------------------- #


def _reject_dups(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` that raises on a duplicate key at any depth."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _load_catalog() -> dict[str, object]:
    """Load the en_US catalog body from the installed package data.

    Mirrors ``tests/test_imports.py``'s wheel-data canary so a packaging
    drift breaks here too rather than silently falling back to the source
    tree. Does NOT reject duplicate JSON keys — use ``_load_catalog_strict``
    for that.
    """
    catalog_path = resources.files("ryzic.i18n.locales") / "en_US.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    return payload["en_US"]


def _load_catalog_strict() -> dict[str, object]:
    """``_load_catalog`` but reject duplicate JSON keys at any depth.

    Test-side only. The runtime loader (``src/ryzic/i18n/__init__.py``)
    intentionally logs and falls back rather than raising on a malformed
    catalog — raising would block bot boot on a typo.
    """
    catalog_path = resources.files("ryzic.i18n.locales") / "en_US.json"
    raw = catalog_path.read_text(encoding="utf-8")
    payload = json.loads(raw, object_pairs_hook=_reject_dups)
    return payload["en_US"]


# --------------------------------------------------------------------------- #
# Render-site walker
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderSite:
    """A single ``t()`` / ``_broadcast_t()`` / ``FetchFailed(...)`` construction.

    ``key`` is ``None`` when the first positional arg is not a string literal
    (dynamic dispatch — e.g. ``t(exc.key, ...)`` or ``FetchFailed(key)``).
    ``literal_kwargs`` maps named kwarg names to their value nodes, EXCLUDING
    ``**`` unpacks (those set ``has_starstar_unpack`` instead, since the
    spilled names can't be verified statically).
    """

    path: Path
    node: ast.Call
    key: str | None
    kind: str
    literal_kwargs: dict[str, ast.expr]
    has_starstar_unpack: bool


def _render_kind(func: ast.expr) -> str | None:
    """Normalize a call's func node to a render kind, or ``None`` if not a render site."""
    if isinstance(func, ast.Name):
        if func.id in _RENDER_KINDS:
            return _RENDER_KINDS[func.id]
        if func.id == "FetchFailed":
            return "fetchfailed"
    elif isinstance(func, ast.Attribute) and func.attr == "FetchFailed":
        return "fetchfailed"
    return None


def _walk_render_sites(roots: Iterable[Path]) -> Iterator[RenderSite]:
    """Yield every ``t()`` / ``_broadcast_t()`` / ``FetchFailed()`` construction.

    Covers both literal and dynamic first args; ``key`` is ``None`` for
    dynamic sites. One walker, one extra branch for ``FetchFailed`` — DRY
    with the ``t()`` lint.
    """
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                kind = _render_kind(node.func)
                if kind is None:
                    continue
                first = node.args[0] if node.args else None
                key: str | None = None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    key = first.value
                literal_kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
                has_starstar = any(kw.arg is None for kw in node.keywords)
                yield RenderSite(path, node, key, kind, literal_kwargs, has_starstar)


def _site_loc(site: RenderSite) -> str:
    """``path:lineno`` for a site, tolerating synthetic nodes with ``lineno=None``."""
    try:
        rel: Path | str = site.path.relative_to(_REPO_ROOT)
    except ValueError:
        rel = site.path
    lineno = site.node.lineno
    return f"{rel}:{lineno if lineno is not None else '<synthetic>'}"


# --------------------------------------------------------------------------- #
# Dynamic FetchFailed site recognition (AST identity, not line number)
# --------------------------------------------------------------------------- #
#
# The one dynamic ``raise FetchFailed(key)`` site in ``ytdlp.py`` is
# recognized by the AST shape of its key's provenance — a bare ``Name``
# first arg bound in the enclosing function scope by an assignment of the
# exact shape ``var = next(<GeneratorExp over _FRIENDLY_ERROR_KEYS.items()>,
# <optional default>)``. Recognizing by shape (never by ``lineno``) makes
# the locator robust to benign line shifts above the raise. Any other
# dynamic site — one whose key is not bound by that shape — falls through
# to the empty ``_DYNAMIC_FETCHFAILED_ALLOWLIST`` and fails the gate loud,
# the fail-loud-by-default contract issue #220 requires.


def _enclosing_function_by_lineno(
    tree: ast.AST, lineno: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function whose source range contains ``lineno``, or ``None``.

    Among all functions whose ``[lineno, end_lineno]`` contains ``lineno``,
    the innermost is the one with the greatest start line (a nested def
    always starts after its enclosing def).
    """
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = node.end_lineno or start
        if start <= lineno <= end and (best is None or start > best.lineno):
            best = node
    return best


def _iter_body_stmts(stmts: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements within a function body, recursing into compound
    statement bodies (``if``/``with``/``try``/``except``/``for``/``while``)
    but NOT into nested ``def``/``class`` bodies.

    The binding and the raise both live in an ``except`` handler body, so
    the iterator must descend into handler bodies. It must NOT descend into
    nested defs: a ``key = next(_FRIENDLY_ERROR_KEYS.items()...)`` inside a
    nested function is a different scope and must not satisfy recognition
    for a raise in the outer function.
    """
    for stmt in stmts:
        yield stmt
        bodies: list[list[ast.stmt]] = []
        if isinstance(stmt, ast.If):
            bodies.append(stmt.body)
            bodies.append(stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            bodies.append(stmt.body)
        elif isinstance(stmt, ast.Try):
            bodies.append(stmt.body)
            bodies.extend(handler.body for handler in stmt.handlers)
            bodies.append(stmt.orelse)
            bodies.append(stmt.finalbody)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            bodies.append(stmt.body)
            bodies.append(stmt.orelse)
        # FunctionDef / AsyncFunctionDef / ClassDef intentionally NOT recursed.
        for body in bodies:
            yield from _iter_body_stmts(body)


def _is_friendly_error_next_call(value: ast.expr) -> bool:
    """True iff ``value`` is ``next(<GeneratorExp>, <optional default>)``
    whose single comprehension's iterable is
    ``<_FRIENDLY_ERROR_KEYS_NAME>.items()`` AND the comprehension carries at
    least one ``if``-filter.

    STRICT by design: matches the exact binding shape at ``ytdlp.py`` —
    ``key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() if ...), None)``.
    The ``if``-filter is load-bearing (without it ``key`` is always the first
    dict value, not the substring match), so a filter-less genexp is rejected.
    Also rejects for-loops, walrus (``NamedExpr``), and ``.keys()``/direct
    iteration; those refactors trip the gate loud by default (issue #220's
    fail-loud contract), forcing a re-review rather than silently blessing a
    different resolution path.
    """
    if not isinstance(value, ast.Call):
        return False
    if not isinstance(value.func, ast.Name) or value.func.id != "next":
        return False
    if value.keywords or not value.args:
        return False
    gen = value.args[0]
    if not isinstance(gen, ast.GeneratorExp) or len(gen.generators) != 1:
        return False
    comp = gen.generators[0]
    if not comp.ifs:
        return False  # the `if substr in detail` filter is load-bearing
    iter_call = comp.iter
    if not isinstance(iter_call, ast.Call) or iter_call.keywords:
        return False
    attr = iter_call.func
    return (
        isinstance(attr, ast.Attribute)
        and attr.attr == "items"
        and isinstance(attr.value, ast.Name)
        and attr.value.id == _FRIENDLY_ERROR_KEYS_NAME
    )


def _recognized_friendly_error_dynamic_fetchfailed_site(site: RenderSite, tree: ast.AST) -> bool:
    """True iff ``site`` is the dynamic ``FetchFailed(<Name>)`` raise whose
    ``<Name>`` is bound in its enclosing function scope by a SINGLE,
    unambiguous assignment of the exact shape ``var = next(<GeneratorExp over
    _FRIENDLY_ERROR_KEYS.items()> with an ``if``-filter, <optional default>)``.

    Accepts a pre-parsed ``tree`` (the ``ytdlp.py`` module tree) so meta-tests
    can feed synthetic ``ast.parse(...)`` trees without temp files. The
    enclosing function is found by source range over ``tree`` (never by
    ``id(site.node)``, which would fail across separate parses). The binding
    must be the ONLY assignment to ``<Name>`` before the raise in that
    function's own scope — multiple assignments (e.g. a conditional
    reassignment that hides an unvalidated literal key on one branch) would
    let the gate bless the friendly-shaped branch while never checking the
    other, so they are refused and the site fails loud.
    """
    if site.kind != "fetchfailed" or site.key is not None:
        return False
    args = site.node.args
    if not args or not isinstance(args[0], ast.Name):
        return False
    raise_lineno = site.node.lineno
    if raise_lineno is None:
        return False
    fn = _enclosing_function_by_lineno(tree, raise_lineno)
    if fn is None:
        return False
    var = args[0].id
    binding: ast.expr | None = None
    assignments = 0
    for stmt in _iter_body_stmts(fn.body):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != var:
            continue
        if stmt.lineno >= raise_lineno:
            continue
        assignments += 1
        binding = stmt.value
    if assignments != 1:
        return False  # zero or ambiguous binding — refuse, fail loud
    assert binding is not None  # exactly one assignment above → binding was set
    return _is_friendly_error_next_call(binding)


def _recognized_dynamic_fetchfailed_site(
    sites: Iterable[RenderSite],
) -> tuple[RenderSite, ast.AST]:
    """Recognize and return the one ``_FRIENDLY_ERROR_KEYS``-anchored dynamic
    ``FetchFailed(key)`` raise site among ``sites``, plus the parsed ytdlp tree.

    ``sites`` is the caller's own list (e.g. the gate test's ``all_sites``),
    so the returned ``RenderSite`` is identity-equal to one of its elements —
    letting the caller exclude it from the allowlist check by ``is``. Fails
    loud (assertion) if zero or more than one site matches — the binding
    shape or anchor name changed and a human must re-review. Shared by both
    fetchfailed gate tests so they agree on what counts as "the" dynamic site.
    """
    ytdlp_path = _SRC_ROOT / "ytdlp.py"
    tree = ast.parse(ytdlp_path.read_text(encoding="utf-8"), filename=str(ytdlp_path))
    matched = [
        s
        for s in sites
        if s.key is None
        and s.path == ytdlp_path
        and _recognized_friendly_error_dynamic_fetchfailed_site(s, tree)
    ]
    assert len(matched) == 1, (
        f"expected exactly one _FRIENDLY_ERROR_KEYS-anchored dynamic FetchFailed site, "
        f"found {len(matched)} — the binding shape or anchor name changed; re-review"
    )
    return matched[0], tree


# --------------------------------------------------------------------------- #
# String-literal scan (for orphan check)
# --------------------------------------------------------------------------- #


def _string_literals_in_tree(tree: ast.AST) -> set[str]:
    """Every string-constant value in ``tree`` EXCEPT docstring expression statements.

    A docstring is an ``ast.Expr`` wrapping a string ``ast.Constant`` (the
    first statement of a module/class/function body — but simplest: skip any
    ``ast.Expr`` whose ``.value`` is a string ``Constant``). AST does NOT see
    ``#`` comments, so comments were never a bless source; docstrings were.
    Excluding docstrings means a dead key kept alive only by a docstring is
    now flagged as an orphan.

    This also collects ``FetchFailed`` first-arg literals, ``_FRIENDLY_ERROR_KEYS``
    dict values, and ternary-assigned keys (``key = "a" if ... else "b"``) —
    all are string ``Constant`` nodes — so live indirectly-referenced keys
    are NOT false-orphaned.
    """
    docstring_ids = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }
    return {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstring_ids
    }


def _string_literals(roots: Iterable[Path]) -> set[str]:
    """Every non-docstring string-constant value appearing in any ``*.py`` under ``roots``."""
    literals: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            literals.update(_string_literals_in_tree(tree))
    return literals


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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


def _friendly_error_key_candidates(tree: ast.AST) -> set[str]:
    """Statically resolve the candidate keys for the dynamic
    ``FetchFailed(key)`` raise site by walking the ``_FRIENDLY_ERROR_KEYS``
    dict literal in ``tree``.

    All values are string literals, so the candidate set is fully determined
    at parse time — no allowlist entry needed. If this dict ever gains a
    non-literal value, the gate fails and forces an explicit resolution path.
    """
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == _FRIENDLY_ERROR_KEYS_NAME
            and isinstance(value, ast.Dict)
        ):
            return {
                v.value
                for v in value.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            }
    return set()


def _check_missing_keys(catalog: Mapping[str, object], sites: Iterable[RenderSite]) -> list[str]:
    problems: list[str] = []
    for site in sites:
        if site.kind not in ("t", "broadcast_t") or site.key is None:
            continue  # dynamic t() dispatch is covered by the orphan-key scan
        if site.key not in catalog:
            problems.append(f"{_site_loc(site)} {site.kind}({site.key!r}) — no such catalog key")
    return problems


def _check_orphans(catalog: Mapping[str, object], refs: set[str]) -> list[str]:
    return [
        f"{key} — orphan catalog key (no caller in src/ or tests/)"
        for key in sorted(set(catalog) - refs)
    ]


def _check_plurals(catalog: Mapping[str, object], allowed_variants: frozenset[str]) -> list[str]:
    problems: list[str] = []
    for key, value in catalog.items():
        if not isinstance(value, dict):
            continue
        variants = set(value.keys())
        unknown = variants - allowed_variants
        if unknown:
            problems.append(f"{key}: unknown plural variants {sorted(unknown)!r}")
        elif "many" not in variants:
            problems.append(f"{key}: plural key missing 'many' (Rails catch-all)")
    return problems


def _check_t_kwargs(catalog: Mapping[str, object], sites: Iterable[RenderSite]) -> list[str]:
    problems: list[str] = []
    for site in sites:
        if site.kind not in ("t", "broadcast_t") or site.key is None or site.key not in catalog:
            continue
        if site.has_starstar_unpack:
            continue  # ``**vars`` spills names we can't verify statically
        expected = _template_placeholders(catalog[site.key])
        provided = {k for k in site.literal_kwargs if k not in _NON_VAR_KWARGS}
        missing = expected - provided
        if missing:
            problems.append(
                f"{_site_loc(site)} {site.kind}({site.key!r}) missing kwargs {sorted(missing)!r} "
                f"(provided: {sorted(provided)!r})"
            )
    return problems


def _check_fetchfailed_keys(
    catalog: Mapping[str, object],
    sites: Iterable[RenderSite],
    allowlist: frozenset[str],
) -> list[str]:
    problems: list[str] = []
    for site in sites:
        if site.kind != "fetchfailed":
            continue
        if site.key is not None:
            if site.key not in catalog:
                problems.append(
                    f"{_site_loc(site)} FetchFailed({site.key!r}) — no such catalog key"
                )
            continue
        if _site_loc(site) in allowlist:
            continue  # explicitly unresolvable; rationale lives with the allowlist entry
        problems.append(
            f"{_site_loc(site)} FetchFailed(<dynamic>) — key not statically resolvable; "
            "add to _DYNAMIC_FETCHFAILED_ALLOWLIST with rationale"
        )
    return problems


def _check_fetchfailed_kwargs(
    catalog: Mapping[str, object], sites: Iterable[RenderSite]
) -> list[str]:
    problems: list[str] = []
    for site in sites:
        if site.kind != "fetchfailed" or site.key is None or site.key not in catalog:
            continue
        if site.has_starstar_unpack:
            continue  # ``**vars`` at a raise site can't be verified statically; allowlist it
        expected = _template_placeholders(catalog[site.key])
        provided = set(site.literal_kwargs)
        missing = expected - provided
        if missing:
            problems.append(
                f"{_site_loc(site)} FetchFailed({site.key!r}) missing kwargs {sorted(missing)!r} "
                f"(provided: {sorted(provided)!r})"
            )
    return problems


def _check_no_duplicate_keys(raw_json: str) -> list[str]:
    try:
        json.loads(raw_json, object_pairs_hook=_reject_dups)
    except ValueError as exc:
        return [str(exc)]
    return []


# --------------------------------------------------------------------------- #
# Gate tests (run against the real catalog + source tree)
# --------------------------------------------------------------------------- #


def test_every_t_call_site_resolves_to_a_catalog_key() -> None:
    """``t("foo.bar")`` with no ``foo.bar`` in the catalog is a guaranteed
    runtime regression: the helper logs at ERROR and returns the dotted
    key string to the user. Catch it at CI."""
    catalog = _load_catalog_strict()
    problems = _check_missing_keys(catalog, _walk_render_sites([_SRC_ROOT]))
    assert not problems, "Missing catalog keys:\n  " + "\n  ".join(problems)


def test_every_catalog_key_has_a_caller() -> None:
    """Reverse direction: orphan keys after a PR cleanup are catalog rot.

    We accept any non-docstring string-literal match in ``src/`` or
    ``tests/`` — that covers indirect references (``_FRIENDLY_ERROR_KEYS``
    mapping values, ternary-assigned keys, test assertions that hard-code
    the key, etc.) without forcing every catalog key through a static
    ``t()`` call. Docstrings do NOT count: a key mentioned only in a
    docstring is not rendered and is therefore still an orphan.
    """
    catalog = _load_catalog_strict()
    refs = _string_literals([_SRC_ROOT, _TESTS_ROOT])
    problems = _check_orphans(catalog, refs)
    assert not problems, "Orphan catalog keys (no caller found):\n  " + "\n  ".join(problems)


def test_plural_keys_use_rails_variants_with_many() -> None:
    """i18nice uses Rails ``many``, not CLDR ``other`` — the latter
    silently returns the bare key. Any plural-shaped catalog value must
    use only Rails variant names AND include ``many`` as the catch-all.
    ``two`` is rejected because i18nice's ``pluralize`` has no ``two``
    codepath (``count == 2`` falls through to ``few``).
    """
    catalog = _load_catalog_strict()
    problems = _check_plurals(catalog, _RAILS_VARIANTS)
    assert not problems, "Rails-plural lint:\n  " + "\n  ".join(problems)


def test_every_template_placeholder_is_passed_at_the_call_site() -> None:
    """Each ``%{name}`` in a catalog body must arrive as a ``name=...`` kwarg.

    Skips ``locale=`` and ``default=`` (consumed by ``t()`` itself, not
    interpolated into the template) and skips calls whose key is not in
    the catalog (already flagged by the missing-from-catalog test).
    """
    catalog = _load_catalog_strict()
    problems = _check_t_kwargs(catalog, _walk_render_sites([_SRC_ROOT]))
    assert not problems, "Variable-set lint:\n  " + "\n  ".join(problems)


def test_every_fetchfailed_key_resolves_to_a_catalog_key() -> None:
    """``FetchFailed(key, **vars)`` carries a catalog key (#163 contract).
    Every construction site — src raise sites AND test fixtures — must
    resolve to a real catalog key. Literal keys are checked directly; the
    one dynamic site is resolved via the ``_FRIENDLY_ERROR_KEYS`` dict
    literal (recognition logic in ``_recognized_dynamic_fetchfailed_site``).
    """
    catalog = _load_catalog_strict()
    all_sites = [s for s in _walk_render_sites([_SRC_ROOT, _TESTS_ROOT]) if s.kind == "fetchfailed"]
    # The known dynamic site is resolved via _FRIENDLY_ERROR_KEYS; recognize it
    # by AST identity and exclude it from the helper (which would otherwise
    # flag it as an unallowlisted dynamic site). Verify its candidates separately.
    matched_site, ytdlp_tree = _recognized_dynamic_fetchfailed_site(all_sites)
    helper_sites = [s for s in all_sites if s is not matched_site]
    problems = _check_fetchfailed_keys(catalog, helper_sites, _DYNAMIC_FETCHFAILED_ALLOWLIST)
    candidates = _friendly_error_key_candidates(ytdlp_tree)
    if not candidates:
        problems.append(
            f"{_site_loc(matched_site)} — could not resolve _FRIENDLY_ERROR_KEYS "
            "dict literal; the dynamic FetchFailed site is no longer statically resolvable"
        )
    for candidate in sorted(candidates):
        if candidate not in catalog:
            problems.append(
                f"{_site_loc(matched_site)} candidate {candidate!r} — no such catalog key"
            )
    assert not problems, "FetchFailed key lint:\n  " + "\n  ".join(problems)


def test_every_fetchfailed_placeholder_is_passed_at_the_raise_site() -> None:
    """Each ``%{name}`` in a ``FetchFailed`` template must arrive as a
    ``name=...`` kwarg at the construction site. The dynamic
    ``FetchFailed(key)`` site passes no kwargs, so every candidate template
    there must be placeholder-free. Extra kwargs are accepted (parity with
    the ``t()`` lint). ``FetchFailed.__init__``'s internal ``t(key, **vars)``
    is NOT walked — it is covered transitively by this raise-site check.
    """
    catalog = _load_catalog_strict()
    sites = list(_walk_render_sites([_SRC_ROOT, _TESTS_ROOT]))
    problems = _check_fetchfailed_kwargs(catalog, sites)
    matched_site, ytdlp_tree = _recognized_dynamic_fetchfailed_site(
        [s for s in sites if s.kind == "fetchfailed"]
    )
    candidates = _friendly_error_key_candidates(ytdlp_tree)
    for candidate in sorted(candidates):
        placeholders = _template_placeholders(catalog.get(candidate))
        if placeholders:
            problems.append(
                f"{_site_loc(matched_site)} candidate {candidate!r} has placeholders "
                f"{sorted(placeholders)!r} but the raise site passes no kwargs"
            )
    assert not problems, "FetchFailed kwarg lint:\n  " + "\n  ".join(problems)


def test_catalog_has_no_duplicate_keys() -> None:
    """Duplicate JSON keys at any depth are a silent catalog rot source
    (``json.loads`` keeps the last value). The strict loader raises."""
    catalog_path = resources.files("ryzic.i18n.locales") / "en_US.json"
    raw = catalog_path.read_text(encoding="utf-8")
    problems = _check_no_duplicate_keys(raw)
    assert not problems, "Duplicate catalog keys:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize(
    "key",
    sorted(k for k, v in _load_catalog_strict().items() if isinstance(v, dict)),
)
def test_plural_keys_pass_count_at_every_call_site(key: str) -> None:
    """Defensive: every plural-shaped key needs ``count=...`` at the call
    site (i18nice's selector). Easy to forget; runtime symptom is silent
    fallback to the ``one`` branch."""
    misses: list[str] = []
    for site in _walk_render_sites([_SRC_ROOT]):
        if site.kind not in ("t", "broadcast_t") or site.key != key:
            continue
        if "count" not in site.literal_kwargs:
            misses.append(f"{_site_loc(site)} {site.kind}({key!r}) missing count=")
    assert not misses, "Plural call without count:\n  " + "\n  ".join(misses)


# --------------------------------------------------------------------------- #
# Meta-tests (gate logic catches synthetic violations)
# --------------------------------------------------------------------------- #


def _synthetic_site(code: str, *, kind: str = "t", path: str = "<synthetic>") -> RenderSite:
    """Build a ``RenderSite`` from an in-memory ``code`` snippet (no temp files).

    ``kind`` is forced — the snippet's func name need not match — so meta-tests
    can construct fetchfailed sites without importing ``FetchFailed``.
    """
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        first = node.args[0] if node.args else None
        key = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else None
        )
        literal_kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        has_starstar = any(kw.arg is None for kw in node.keywords)
        return RenderSite(Path(path), node, key, kind, literal_kwargs, has_starstar)
    raise AssertionError(f"no Call node in synthetic snippet: {code!r}")


def _synthetic_fetchfailed_site(tree: ast.AST, *, path: str = "<synthetic>") -> RenderSite:
    """Build a ``RenderSite`` from the ``FetchFailed(...)`` Call in ``tree``.

    Unlike ``_synthetic_site`` (which grabs the first ``Call`` and forces a
    kind), this locates the ``FetchFailed``-kind Call via ``_render_kind`` so
    a richer snippet — e.g. one that also contains a ``next(...)`` and an
    ``.items()`` call — yields the FetchFailed site, not whichever Call
    ``ast.walk`` happens to visit first. The returned site's ``node`` belongs
    to the SAME tree the caller passes to the recognizer.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _render_kind(node.func) != "fetchfailed":
            continue
        first = node.args[0] if node.args else None
        key = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else None
        )
        literal_kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        has_starstar = any(kw.arg is None for kw in node.keywords)
        return RenderSite(Path(path), node, key, "fetchfailed", literal_kwargs, has_starstar)
    raise AssertionError("no FetchFailed Call in synthetic tree")


def test_meta_gate_catches_missing_key() -> None:
    site = _synthetic_site('t("foo.bar")')
    problems = _check_missing_keys({"other": "x"}, [site])
    assert problems and "foo.bar" in problems[0]


def test_meta_gate_catches_orphan_key() -> None:
    """A key referenced ONLY in a docstring is flagged — docstrings are
    excluded from the orphan-check reference set."""
    tree = ast.parse('"""module docstring mentioning foo.bar and nothing else"""')
    refs = _string_literals_in_tree(tree)
    assert "foo.bar" not in refs, "docstring string leaked into the reference set"
    problems = _check_orphans({"foo.bar": "x"}, refs)
    assert problems and any("foo.bar" in p for p in problems)


def test_meta_gate_catches_bad_plural() -> None:
    catalog = {"k": {"one": "x", "other": "y"}}
    problems = _check_plurals(catalog, _RAILS_VARIANTS)
    assert problems and any("other" in p for p in problems)


def test_meta_gate_catches_missing_kwarg() -> None:
    catalog = {"k": "hello %{name}"}
    site = _synthetic_site('t("k")')
    problems = _check_t_kwargs(catalog, [site])
    assert problems and "name" in problems[0]


def test_meta_gate_catches_typo_fetchfailed_key() -> None:
    catalog = {"ytdlp.error.real": "x"}
    site = _synthetic_site('FetchFailed("ytdlp.error.bogus")', kind="fetchfailed")
    problems = _check_fetchfailed_keys(catalog, [site], _DYNAMIC_FETCHFAILED_ALLOWLIST)
    assert problems and "ytdlp.error.bogus" in problems[0]


def test_meta_gate_recognizes_friendly_error_dynamic_fetchfailed_site() -> None:
    """The recognizer accepts a dynamic ``FetchFailed(key)`` whose ``key`` is
    bound by the exact ``next(<genexp over _FRIENDLY_ERROR_KEYS.items()>)``
    shape in the enclosing function — the positive path with zero coverage
    before this lock."""
    tree = ast.parse(
        "async def _extract():\n"
        "    try:\n"
        "        pass\n"
        "    except YoutubeDLError as exc:\n"
        "        detail = ''\n"
        "        key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() "
        "if substr in detail), None)\n"
        "        raise FetchFailed(key) from exc\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_rejects_foreign_dynamic_fetchfailed_site() -> None:
    """A dynamic ``FetchFailed(key)`` whose ``key`` is NOT bound by the
    ``_FRIENDLY_ERROR_KEYS`` ``next()`` shape is rejected by the recognizer
    and then flagged by ``_check_fetchfailed_keys`` via the empty allowlist
    (the two-step path a future unrelated dynamic site hits)."""
    tree = ast.parse("async def fn():\n    key = something_else()\n    raise FetchFailed(key)\n")
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)
    problems = _check_fetchfailed_keys({"x": "y"}, [site], _DYNAMIC_FETCHFAILED_ALLOWLIST)
    assert problems and "dynamic" in problems[0]


def test_meta_gate_rejects_refactored_friendly_error_binding() -> None:
    """A for-loop binding of ``key`` is rejected: the recognizer accepts only
    the ``next(<genexp over _FRIENDLY_ERROR_KEYS.items()>)`` shape, and the
    for-loop's ``key = k`` is an ``ast.Name``, not a ``next()`` call."""
    tree = ast.parse(
        "async def fn():\n"
        "    detail = ''\n"
        "    key = None\n"
        "    for substr, k in _FRIENDLY_ERROR_KEYS.items():\n"
        "        if substr in detail:\n"
        "            key = k\n"
        "            break\n"
        "    raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_rejects_conditional_reassignment_of_friendly_binding() -> None:
    """Two assignments to ``key`` (a literal branch + a friendly-shaped branch)
    are refused — the dangerous direction the for-loop test does not cover,
    where a friendly-shaped branch would otherwise be blessed while the
    literal branch's key went unchecked."""
    tree = ast.parse(
        "async def fn():\n"
        "    detail = ''\n"
        "    if cond:\n"
        "        key = 'ytdlp.error.fixed_key'\n"
        "    else:\n"
        "        key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() "
        "if substr in detail), None)\n"
        "    raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_rejects_nested_def_scope_binding() -> None:
    """Locks the ``_iter_body_stmts`` non-descent-into-defs guard: a binding
    inside a nested ``def`` must not satisfy recognition for an outer-scope
    raise. This synthetic test is the only safeguard — the real ``ytdlp.py``
    site has no nested def, so the live gate test can't catch a loosening."""
    tree = ast.parse(
        "async def outer():\n"
        "    detail = ''\n"
        "    def inner():\n"
        "        key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() "
        "if substr in detail), None)\n"
        "    inner()\n"
        "    raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_rejects_walrus_named_expr_binding() -> None:
    """A walrus binding (``key := next(...)``) is rejected: the binding loop
    inspects only ``ast.Assign``, so a ``NamedExpr`` embedded in an ``if`` test
    is never found. Locks the Assign-only property, which is structurally
    distinct from the for-loop rejection (a widened recognizer accepting
    walrus would pass the for-loop test while silently blessing walrus)."""
    tree = ast.parse(
        "async def fn():\n"
        "    detail = ''\n"
        "    if (key := next((k for substr, k in _FRIENDLY_ERROR_KEYS.items() "
        "if substr in detail), None)) is not None:\n"
        "        raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


@pytest.mark.parametrize(
    "binding",
    [
        # .keys() instead of .items()
        "next((k for substr, k in _FRIENDLY_ERROR_KEYS.keys() if substr in detail), None)",
        # .values() instead of .items()
        "next((k for k in _FRIENDLY_ERROR_KEYS.values() if k in detail), None)",
        # direct iteration of the dict instead of .items()
        "next((k for k in _FRIENDLY_ERROR_KEYS if k in detail), None)",
    ],
    ids=["keys", "values", "bare-iter"],
)
def test_meta_gate_rejects_non_items_iterable_on_friendly_binding(binding: str) -> None:
    """The anchor requires ``.items()``; ``.keys()``, ``.values()``, and direct
    iteration of ``_FRIENDLY_ERROR_KEYS`` are all rejected."""
    tree = ast.parse(
        f"async def fn():\n    detail = ''\n    key = {binding}\n    raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_rejects_filterless_friendly_error_genexp() -> None:
    """A ``next(<genexp>)`` without the ``if``-filter is rejected — the filter
    is load-bearing (see ``_is_friendly_error_next_call``)."""
    tree = ast.parse(
        "async def fn():\n"
        "    key = next((k for substr, k in _FRIENDLY_ERROR_KEYS.items()), None)\n"
        "    raise FetchFailed(key)\n"
    )
    site = _synthetic_fetchfailed_site(tree)
    assert not _recognized_friendly_error_dynamic_fetchfailed_site(site, tree)


def test_meta_gate_catches_duplicate_catalog_key() -> None:
    problems = _check_no_duplicate_keys('{"en_US": {"a": 1, "a": 2}}')
    assert problems and "a" in problems[0]
