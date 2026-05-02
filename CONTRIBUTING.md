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
5. PRs need at least one approving review before merge. Don't self-merge.

Before-merge gates (run as separate review passes on substantial PRs):

- `/review` — correctness + design.
- `/security-review` — input handling, supply chain, secrets exposure.
- `/simplify` — KISS / DRY / SRP / SOLID housekeeping.

## Where the design lives

The M1 plan and the pre-implementation plan reviews live under [docs/plans/](docs/plans/). `M1.md` is the source of truth for behavior, env vars, and module layout; `M1-review.md` and `M1-simplify.md` capture the pre-implementation correctness/security/UX critique and KISS pass.

Per-PR review output (`/review`, `/security-review`, `/simplify`) is posted as PR comments on the GitHub PR — not committed to the repo. Reference the merged PR for any context on the review verdicts behind a given change.

Forward-looking planning lives in GitHub Issues. See [#13](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/13) (M1 epic) and [#8](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/8) (M2 epic).

## What we won't merge

- Anything that enables YouTube cookies or a credentialed-fetch path without a security review and a clear opt-in. See [README.md § Self-hoster considerations](README.md#self-hoster-considerations).
- Secrets in commits (tokens, `.env`, signing keys). `.gitignore` lists the obvious patterns; if you find yourself fighting it, stop and ask.
- New runtime dependencies without a justification in the PR body.

## Manual smoke tests

[docs/manual-smoke-tests.md](docs/manual-smoke-tests.md) is the checklist run before each release. PRs that touch the playback path should at minimum re-run the **Core playback** section locally and call out the result in the PR description.
