# Versioning policy

ryzic follows [Semantic Versioning 2.0.0](https://semver.org/) with the pre-1.0 caveats spelled out below. Releases are cut by [release-please](https://github.com/googleapis/release-please) from Conventional Commits on `main`.

## Pre-1.0 (`0.x.y`)

While the major version is `0`, **minor bumps may contain breaking changes**. release-please uses the standard `release-type: python` mapping pre-1.0:

- `fix:` → patch (`0.x.Y+1`)
- `feat:` → minor (`0.MINOR+1.0`)
- `feat!:` / `BREAKING CHANGE:` → minor pre-1.0 rather than a major bump (the major version stays at `0` until the bot is declared stable; the loud "this might break you" signal is the minor bump itself)

Post-1.0, the standard SemVer mapping applies (`feat:` → minor, `feat!:` → major).

If stability matters to you before v1.0, **pin to an exact version** (`ryzic==0.4.2`) and read [CHANGELOG.md](CHANGELOG.md) before upgrading. The CHANGELOG calls out breaking changes with a `BREAKING CHANGE:` footer.

Patch versions remain non-breaking even pre-1.0.

## v1.0+ stable surfaces

When ryzic reaches v1.0, the following surfaces become covered by SemVer guarantees. Breaking changes to any of them require a major version bump.

- **Slash command names.** `/play`, `/skip`, `/queue`, `/pause`, `/resume`, `/leave` and any commands added later won't be renamed without a major bump.
- **Slash command parameter shapes.** Names, types, optionality, and ordering of parameters are stable.
- **Environment variable names and meanings.** The configuration contract is `DISCORD_BOT_TOKEN`, `LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_PASSWORD`, `RYZIC_CACHE_DIR`, `RYZIC_CACHE_MAX_GB`, `RYZIC_LOG_LEVEL`, `RYZIC_GUILD_IDS`, and `RYZIC_YOUTUBE_COOKIES_PATH`. Renaming any of them, or changing the units of `RYZIC_CACHE_MAX_GB`, is breaking. Adding new optional env vars with sensible defaults is not.
- **Default behaviors observable from a Discord client.** The auto-leave timeout, queue cap, and per-track duration cap are documented defaults; changing them in a way that surprises a self-hoster running a stable upgrade is breaking.
- **`compose.yaml` rolling-upgrade compatibility.** A `docker compose pull && docker compose up -d` against an unchanged user-supplied `.env` should continue to work across patch and minor versions.
- **On-disk cache format.** The SQLite schema and audio file layout under `RYZIC_CACHE_DIR` are stable; ryzic will migrate old caches in place rather than ignore them.
- **Minimum Python and Docker versions.** Raising either is breaking — operators may not have a newer interpreter or engine available.

The following are **explicitly not stable** and may change in any release:

- Internal Python APIs (anything under `src/ryzic/` consumed by import). ryzic is an application, not a library; we don't promise downstream importers anything.
- Lavalink protocol details, plugin pins (`youtube-source` version), and `lavalink/application.yml` shape — these track upstream Lavalink and may move with it.
- Log message wording, log level of any specific event, embed text and exact wording of error messages. Don't grep logs in CI; don't pattern-match embed text from another bot.

## Deprecation policy (post-1.0)

When we plan to remove or rename a stable surface, we deprecate it for **at least one full minor version** before removing. Deprecated env vars continue to work and emit a `WARNING` log; deprecated commands keep working but their description prefixes with `[deprecated]`.

A removal that wasn't preceded by a deprecation cycle is itself a bug — please file an issue.
