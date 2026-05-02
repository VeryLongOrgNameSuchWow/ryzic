# Versioning policy

ryzic follows [Semantic Versioning 2.0.0](https://semver.org/) with the pre-1.0 caveats spelled out below. Releases are cut by [release-please](https://github.com/googleapis/release-please) from Conventional Commits on `main`.

## Pre-1.0 (`0.x.y`)

While the major version is `0`, **minor bumps may contain breaking changes**. release-please is configured with `bump-minor-pre-major: true`, so a `feat!:` commit produces a `0.MINOR+1.0` release rather than a major bump.

If stability matters to you before v1.0, **pin to an exact version** (`ryzic==0.4.2`, `ghcr.io/.../ryzic:0.4.2`) and read [CHANGELOG.md](CHANGELOG.md) before upgrading. The CHANGELOG calls out breaking changes with a `BREAKING CHANGE:` footer.

Patch versions (`0.x.Y+1`) remain non-breaking even pre-1.0.

## v1.0+ stable surfaces

When ryzic reaches v1.0, the following surfaces become covered by SemVer guarantees. Breaking changes to any of them require a major version bump.

- **Slash command names.** `/play`, `/skip`, `/queue`, `/pause`, `/resume`, `/leave` and any commands added later won't be renamed without a major bump.
- **Slash command parameter shapes.** Names, types, optionality, and ordering of parameters are stable.
- **Environment variable names and meanings.** Renaming `RYZIC_CACHE_DIR` or changing the units of `RYZIC_CACHE_MAX_GB` is breaking. Adding new optional env vars with sensible defaults is not.
- **Default behaviors observable from a Discord client.** The auto-leave timeout, queue cap, and per-track duration cap are documented defaults; changing them in a way that surprises a self-hoster running a stable upgrade is breaking.
- **`compose.yaml` rolling-upgrade compatibility.** A `docker compose pull && docker compose up -d` against an unchanged user-supplied `.env` should continue to work across patch and minor versions.
- **On-disk cache format.** The SQLite schema and audio file layout under `RYZIC_CACHE_DIR` are stable; ryzic will migrate old caches in place rather than ignore them.

The following are **explicitly not stable** and may change in any release:

- Internal Python APIs (anything under `src/ryzic/` consumed by import). ryzic is an application, not a library; we don't promise downstream importers anything.
- Lavalink protocol details, plugin pins (`youtube-source` version), and `lavalink/application.yml` shape — these track upstream Lavalink and may move with it.
- Log message wording, log level of any specific event, embed text and exact wording of error messages. Don't grep logs in CI; don't pattern-match embed text from another bot.

## Deprecation policy (post-1.0)

When we plan to remove or rename a stable surface, we deprecate it for **at least one full minor version** before removing. Deprecated env vars continue to work and emit a `WARNING` log; deprecated commands keep working but their description prefixes with `[deprecated]`.

A removal that wasn't preceded by a deprecation cycle is itself a bug — please file an issue.

## Major-bump triggers

Any of the following requires a major version bump (post-1.0):

- Removing or renaming a slash command.
- Renaming or repurposing an environment variable.
- Breaking the on-disk cache format in a way that loses data on upgrade.
- Breaking `compose.yaml` rolling upgrades (e.g. requiring a new manual `.env` value with no default, or a compose schema change that user-edited copies can't tolerate).
- Raising the minimum Python or Docker version.

Routine changes that do **not** require a major bump:

- Adding a new slash command, env var, or optional parameter (with a sensible default).
- Bumping the pinned Lavalink server image within its 4.x line.
- Internal refactors visible only to importers of `src/ryzic/`.
