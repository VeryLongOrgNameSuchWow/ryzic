# ryzic

[![CI](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/ci.yml/badge.svg)](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/ci.yml)
[![CodeQL](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/codeql.yml/badge.svg)](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/codeql.yml)
[![Latest release](https://img.shields.io/github/v/release/VeryLongOrgNameSuchWow/ryzic?include_prereleases&sort=semver)](https://github.com/VeryLongOrgNameSuchWow/ryzic/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

A self-hostable Discord music bot. Plays YouTube audio in voice channels via [Lavalink](https://lavalink.dev/), with a local LRU cache so frequently-played tracks survive yt-dlp breakage. One bot per server you run yourself; no public hosting, no telemetry, no premium tier.

## Features

- Six slash commands: `/play`, `/skip`, `/queue`, `/pause`, `/resume`, `/leave`.
- Per-guild queue with paged display.
- LRU audio cache backed by SQLite — re-playing a track skips the network round-trip and survives transient yt-dlp regressions.
- Auto-leave after 5 minutes idle in voice (sensible Discord etiquette).
- One-command deploy via `docker compose`.
- No privileged Discord intents required.

## Requirements

- Docker 24+ with `docker compose` v2 (the newer `compose.yaml` shape).
- A Discord account that can create applications.
- A voice channel you can invite the bot into.

## Setup

### 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. In **Bot** → **Token**, click **Reset Token** and copy it. You'll paste this into `.env` in step 4.
3. **Privileged Gateway Intents — leave them all OFF.** ryzic does not need Server Members, Message Content, or Presence. If you turned them on, turn them off.
4. In **OAuth2** → **URL Generator**:
   - Scopes: `bot`, `applications.commands`.
   - Bot Permissions: `Connect`, `Speak`, `Send Messages`, `Embed Links`.
   - **Do not pick Administrator.** A music bot has no business with admin rights — it's a security smell and many server owners refuse such invites.
5. Open the generated URL and invite the bot to your server.

### 2. Fetch the deploy files

You only need three files on disk — the published [`ghcr.io/verylongorgnamesuchwow/ryzic`](https://github.com/VeryLongOrgNameSuchWow/ryzic/pkgs/container/ryzic) image carries the bot itself, so there's no source tree to maintain.

```bash
mkdir ryzic && cd ryzic
curl -fsSLO https://raw.githubusercontent.com/VeryLongOrgNameSuchWow/ryzic/main/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/VeryLongOrgNameSuchWow/ryzic/main/.env.example
mkdir lavalink && curl -fsSL -o lavalink/application.yml \
  https://raw.githubusercontent.com/VeryLongOrgNameSuchWow/ryzic/main/lavalink/application.yml
mv .env.example .env
```

Edit `.env` and paste the token into `DISCORD_BOT_TOKEN=`.

(Optional — strongly recommended during first-run testing.) Add a comma-separated list of guild IDs to register slash commands instantly. Without it Discord propagates new commands globally, which can take up to an hour:

```bash
RYZIC_GUILD_IDS=123456789012345678
```

### 3. Bring it up

```bash
docker compose up -d
```

The first boot pulls the ryzic + Lavalink images and downloads the `youtube-source` plugin (~30s). Tail the logs to confirm both services are healthy:

```bash
docker compose logs -f
```

`compose.yaml` pins the bot to the `:0.1` minor tag — `docker compose pull && docker compose up -d` picks up patch releases on the 0.1.x line. Bumping to a future `:0.2` is a manual edit, since pre-1.0 minor bumps may include breaking changes (see [SEMVER.md](SEMVER.md)).

### 4. Try it

Join a voice channel, then in any text channel the bot can see:

```
/play https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

You should hear audio within a couple of seconds.

### Troubleshooting

- **Slash commands don't appear.** Discord caches global commands for ~1h. Set `RYZIC_GUILD_IDS` to the test guild's ID and `docker compose restart ryzic` for instant registration.
- **"Audio service is down."** Check `docker compose logs lavalink`. The bot expects Lavalink at `lavalink:2333` inside the compose network — if you've changed `LAVALINK_HOST` or `LAVALINK_PORT`, both services need to agree.
- **No audio plays despite "Queued".** ryzic only joins your voice channel — make sure you joined first, and that the bot has `Connect` and `Speak` on it.
- **`Failed to load: ...`** Some videos are age-restricted, region-locked, or private. ryzic surfaces a friendly variant of yt-dlp's error; the cause is upstream.
- **You changed `RYZIC_CACHE_DIR`.** Prefer changing only the host-side path of the bind mount (top-level `volumes:` block) and leaving the in-container path alone. If you must change the in-container path, update the env value **and** both `volumes:` mount targets (ryzic and lavalink), and keep the lavalink mount `:ro`. A mismatch here means ryzic writes files Lavalink can't find.

## Configuration

All configuration is via environment variables (read from `.env` by `docker compose`).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | Bot token from the Discord Developer Portal. |
| `LAVALINK_PASSWORD` | no | `youshallnotpass` | Must match `lavalink/application.yml`. Change both together. |
| `LAVALINK_HOST` | no | `lavalink` | Set automatically by `compose.yaml`. Override only when running ryzic outside compose. |
| `LAVALINK_PORT` | no | `2333` | |
| `RYZIC_CACHE_DIR` | no | `/var/cache/ryzic` (compose) / `./.cache` (local) | Audio + playlist cache directory. **Both services must mount the same path.** |
| `RYZIC_CACHE_MAX_GB` | no | `5` | LRU eviction kicks in once cached audio exceeds this size (GiB). |
| `RYZIC_LOG_LEVEL` | no | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `RYZIC_GUILD_IDS` | no | unset | Comma-separated guild IDs for instant slash-command registration. Unset = global registration (up to 1h propagation). |

## Self-hoster considerations

- **YouTube cookies are deliberately disabled.** Enabling them lets a deployment fetch age-restricted/private content under your account, which is a security and account-safety risk. There is no env var to enable them in M1; if you need them, fork and review the change carefully first.
- **The cache directory holds copyrighted material.** What you cache and how long you keep it is your responsibility. Default eviction is by least-recently-used at the `RYZIC_CACHE_MAX_GB` threshold.
- **No rate limiting in this release.** Anyone in your server who can run slash commands can fill your queue or your disk. If you don't trust everyone, restrict command access via Discord's built-in **Server Settings** → **Integrations** permissions before exposing the bot widely.
- **Single-instance only.** The audio cache, playlist cache, and per-guild state assume exactly one ryzic process per cache directory. Don't run two replicas against the same volume.

## Development

You don't need Docker to hack on the code — only to run a real Lavalink (which the integration tests spin up themselves via testcontainers).

```bash
git clone https://github.com/VeryLongOrgNameSuchWow/ryzic.git
cd ryzic
uv sync
uv run pytest -q
uv run python -m ryzic    # requires DISCORD_BOT_TOKEN + a reachable Lavalink
```

To run the local working tree under compose instead of the published GHCR image, layer the dev overlay on top of `compose.yaml`:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

The overlay swaps `image: ghcr.io/...` for `build: .` so iteration doesn't require pushing to GHCR.

Run the same lint/type/test suite CI runs before opening a PR — see [CONTRIBUTING.md § Pull requests](CONTRIBUTING.md#pull-requests) for the canonical command. [docs/manual-smoke-tests.md](docs/manual-smoke-tests.md) is the end-to-end checklist run before each release.

## Versioning

ryzic is pre-1.0; minor bumps may include breaking changes. See [SEMVER.md](SEMVER.md) for the full policy and what counts as breaking once we reach v1.0.

## License

[MIT](LICENSE).
