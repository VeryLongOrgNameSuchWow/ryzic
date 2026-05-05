# Releasing

`ryzic` uses [release-please](https://github.com/googleapis/release-please) to drive versioning, the changelog, and release tags. The user-facing artifact is a Docker image published to GHCR.

## Normal release flow

1. Land conventional-commit PRs on `main`.
2. release-please opens (or refreshes) a release PR titled `chore(main): release X.Y.Z`.
3. `release-please.yml` follows up by rewriting `compose.yaml`'s image pin to `:X.Y` (no-op on patch releases; bumps the pin on minor releases) and pushes the change onto the release PR's branch so the diff is visible at review time.
4. Review the release PR (sanity-check the `CHANGELOG.md` diff, the version bump, and the `compose.yaml` pin if it changed).
5. Merge the release PR.
6. Merging creates the `vX.Y.Z` tag and the GitHub Release with curated notes.
7. The tag push fires `release.yml`, which builds and pushes a Docker image to GHCR with three tags: `:X.Y.Z`, `:X.Y`, `:latest`.

That's it. The cut is hands-off as long as conventional commits are clean.

## Bump table (pre-1.0)

| Commit type | Version bump |
| --- | --- |
| `feat:` | minor (`0.X.0`) |
| `fix:`, `docs:`, `perf:` | patch (`0.X.Y`) |
| `refactor:`, `chore:`, `test:`, `ci:`, `build:` | none |
| `BREAKING CHANGE:` footer or `feat!:` / `fix!:` | minor pre-1.0 (loud signal); major post-1.0 |

This matches release-please's default behavior for `release-type: python` — no `bump-*-pre-major` overrides in `release-please-config.json`.

## Forcing a release

If commits since the last tag don't justify a bump but you want one anyway (e.g. to reset CHANGELOG cadence, or to ship a docs-only release):

```sh
git commit --allow-empty -m "chore: trigger release

Release-As: 0.X.Y"
git push
```

release-please will open a release PR for the specified version on next run.

## release-please authentication (GitHub App)

`release-please.yml` mints an installation token from the `vlonsw-release-please` GitHub App via `actions/create-github-app-token`. We use App auth instead of `GITHUB_TOKEN` because tags + PRs created via `GITHUB_TOKEN` do **not** fire downstream workflows (GitHub anti-loop rule). That would mean release-please's tag push silently skips `release.yml` (the GHCR publish). App-token pushes count as user pushes and bypass that rule.

Repo secrets needed:
- `RELEASE_PLEASE_APP_ID`
- `RELEASE_PLEASE_APP_PRIVATE_KEY`

The App is shared across the maintainer's repos; install it on each repo via the App's settings page.

## Recovering a release PR with 0 CI checks

Release-please PRs created by `GITHUB_TOKEN` (e.g. before App auth was wired) don't fire CI. Push an empty commit to the release branch to retrigger:

```sh
git checkout release-please--branches--main--components--ryzic
git commit --allow-empty -m "chore: retrigger CI"
git push
```

Or close + reopen the PR.

## Manual emergency release

If release-please is broken and you need to ship a hotfix:

1. Bump `version` in `pyproject.toml` manually.
2. Add an entry to `CHANGELOG.md` (Keep-a-Changelog format).
3. Tag locally: `git tag vX.Y.Z`.
4. Push the tag: `git push origin vX.Y.Z`. This fires `release.yml` (GHCR publish + GitHub Release).
5. Open a follow-up PR to update `.release-please-manifest.json` so release-please's next run is consistent with the manual bump.

## Release-dry-run

`release-dry-run.yml` runs on PRs that touch packaging-relevant files (`pyproject.toml`, `uv.lock`, `Dockerfile`, `compose.yaml`, `lavalink/application.yml`, the release workflows themselves). It builds the Docker image to catch regressions before they hit a tag push.
