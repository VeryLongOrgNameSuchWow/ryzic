# Contributing to ryzic

Thanks for taking the time. This file documents the workflow we expect for changes that land on `main`.

## Commits

ryzic uses [Conventional Commits](https://www.conventionalcommits.org/). The commit type drives the version bump that [release-please](https://github.com/googleapis/release-please) cuts. See [SEMVER.md](SEMVER.md) for the pre-1.0 caveats:

- `fix:` → patch release.
- `feat:` → minor release.
- `feat!:` (or any commit with a `BREAKING CHANGE:` footer) → minor release pre-1.0, major release post-1.0.
- `chore:`, `docs:`, `test:`, `refactor:`, `ci:` → no release.

Squash your branch into a single Conventional Commit at merge time, or keep individual commits each conventional — either is fine, as long as the final history on `main` is parseable.

## Pull requests

1. Open an issue first for non-trivial changes — saves both sides time if the design needs a discussion.
2. Branch off `main`. Push to a feature branch (we don't accept pushes directly to `main`).
3. Make sure CI is green: `uv run ruff check && uv run ruff format --check && uv run ty check && uv run pytest -q`.
4. Open a PR. Keep the PR focused — one logical change per PR is much easier to review than a kitchen sink.
5. External PRs get a maintainer review before merge. Maintainer self-merges are OK once CI is green and the review gates below come back clean — there is currently a single maintainer, so an outside approval isn't always available, but the gates are non-negotiable.

Before-merge gates (run as separate review passes on substantial PRs):

- `/review` — correctness + design.
- `/security-review` — input handling, supply chain, secrets exposure.
- `/simplify` — KISS / DRY / SRP / SOLID housekeeping.

## Where the design lives

The M1 plan and the pre-implementation plan reviews live under [docs/plans/](docs/plans/). `M1.md` is the source of truth for behavior, env vars, and module layout; `M1-review.md` and `M1-simplify.md` capture the pre-implementation correctness/security/UX critique and KISS pass.

Per-PR review output (`/review`, `/security-review`, `/simplify`) is posted as PR comments on the GitHub PR — not committed to the repo. Reference the merged PR for any context on the review verdicts behind a given change.

Forward-looking planning lives in GitHub Issues. See [#13](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/13) (M1 epic) and [#8](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/8) (M2 epic).

## User-facing strings (i18n catalog)

Every user-facing string ryzic renders — embed copy, ephemeral error messages, slash-command and parameter descriptions, controller-button labels, Lavalink-broadcast notices — lives in the catalog at `src/ryzic/i18n/locales/en_US.json`. Source code never contains a raw English literal that hits Discord. The wave that landed this is [#13](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/13); the runtime is [i18nice](https://pypi.org/project/i18nice/) (Rails-style placeholders, configurable missing-key/missing-var hooks).

**Adding a new string.**

1. Add the catalog entry. Dot-namespace it `<surface>.<intent>.<variant>` (`play.error.queue_full`, `np.embed.title.idle`, `controller.button.skip`). Variables use Rails syntax: `%{name}` — never Python `{name}`.
2. Render it via `t()`:
   ```python
   from ryzic.i18n import t, locale_for_ephemeral, locale_for_public

   await ctx.respond(
       t("play.error.queue_full", locale=locale_for_ephemeral(ctx), count=n, cap=cap),
       ephemeral=True,
   )
   ```
3. Pick the right locale resolver:
   - `locale_for_ephemeral(ctx)` for ephemeral responses (the invoker is the only viewer; use their interaction locale).
   - `locale_for_public(ctx)` for embeds the whole channel sees (prefer the guild locale; fall back to invoker locale, then `en_US`).
   - `"en_US"` literal for paths with no `ctx` — module-import-time slash-command and parameter descriptions, plus the `_broadcast_t` helper for Lavalink-driven channel broadcasts (auto-leave, track-stuck, voice-lost). These run before any user interaction; there is no "right" locale.

**Slash-command + parameter descriptions** must resolve at module import time because lightbulb evaluates them on the class body, with no `ctx` in scope:

```python
class Play(
    lightbulb.SlashCommand,
    name="play",
    description=t("play.command.description", locale="en_US"),
    ...
):
    url = lightbulb.string(
        "url",
        t("play.param.url.description", locale="en_US"),
        ...
    )
```

The catalog is loaded at `ryzic.i18n` import time, which precedes command-module imports, so this works. If the catalog fails to load, the decorator literal becomes the dotted key string — visible regression, not a crash.

**Markdown safety contract.** When a catalog template wraps a variable in markdown structure (`**%{title}**`, `` `%{value}` ``, `[link](%{url})`), the call site MUST escape the variable with `ux.escape_markdown` before passing it as a kwarg. i18nice does plain string substitution; it does not sanitize markdown. The catalog template owns the markdown wrappers; the call site owns the escaping. Example:

```python
# Catalog: lavalink.broadcast.track_exception = "Track **%{title}** failed: %{detail}. Skipping."
_broadcast_t(
    "lavalink.broadcast.track_exception",
    title=ux.escape_markdown(_safe_error_text(title)),
    detail=ux.escape_markdown(detail),
)
```

Discord mention markup (`<#123>`, `<@456>`, `<t:123:R>`) is rendered client-side and does NOT need escaping — those tokens pass through i18nice and Discord as opaque literals.

**Plurals use Rails variants.** Plural-shaped keys use a dict body keyed by `zero` / `one` / `two` / `few` / `many`. `many` is the required catch-all (i18nice's analogue of CLDR's `other`). Do not use `other` — i18nice silently returns the bare key for that variant. The catalog-drift lint rejects it.

```json
"queue.error.too_few_pages": {
  "one":  "Queue has only 1 page.",
  "many": "Queue has only %{count} pages."
}
```

The call site must always pass `count=...`; it is i18nice's plural selector.

**Event-driven broadcasts (no `ctx`).** Channel notices fired by Lavalink listeners (auto-leave, voice-lost, node-reconnecting, track-stuck, track-exception) use the `_broadcast_t` helper, which pins the locale to `en_US`. There is no interaction in scope on those paths — picking a locale would require a per-guild override (intentionally out of scope; the wave's `out of scope` reminder in the [v2 plan](docs/plans/) holds).

```python
from ryzic.i18n import _broadcast_t

message = _broadcast_t("lavalink.broadcast.voice_lost")
```

**The catalog-drift lint.** `tests/test_i18n_catalog.py` is part of the default `pytest` lane and gates four invariants:

- Every `t("literal")` / `_broadcast_t("literal")` call resolves to a real catalog key.
- Every catalog key is referenced somewhere in `src/` or `tests/` (any string-literal match — covers dynamic indirections like `FetchFailed` raise sites).
- Every plural-shaped key uses only Rails variants and contains `many`.
- Every `%{name}` placeholder in a catalog template arrives as a `name=...` kwarg at the call site.

When the lint fails, the failure message lists the offending `file:line` and the offending key. The fix is mechanical: either add the catalog entry, remove the orphan, fix the plural variant name, or pass the missing kwarg. Run it locally with `uv run pytest tests/test_i18n_catalog.py`.

## What we won't merge

- Anything that enables YouTube cookies or a credentialed-fetch path without a security review and a clear opt-in. See [README.md § Self-hoster considerations](README.md#self-hoster-considerations).
- Secrets in commits (tokens, `.env`, signing keys). `.gitignore` lists the obvious patterns; if you find yourself fighting it, stop and ask.
- New runtime dependencies without a justification in the PR body.

## Action pinning

GitHub Actions pins must use the **commit SHA**, not the annotated tag-object SHA. The Actions runner accepts either, but OSSF Scorecard's webapp verification rejects tag-object SHAs as `imposter commit` and silently tanks the score. To resolve a tag to its target commit:

```bash
git ls-remote https://github.com/<org>/<repo>.git refs/tags/vX.Y.Z^{}
```

The `^{}` suffix peels the annotated tag to the underlying commit. Dependabot already uses commit SHAs by default — this only matters when manually adding a new action pin.

## Manual smoke tests

[docs/manual-smoke-tests.md](docs/manual-smoke-tests.md) is the checklist run before each release. PRs that touch the playback path should at minimum re-run the **Core playback** section locally and call out the result in the PR description.
