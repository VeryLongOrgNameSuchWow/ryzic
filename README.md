# ryzic

[![CI](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/ci.yml/badge.svg)](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/ci.yml)
[![CodeQL](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/codeql.yml/badge.svg)](https://github.com/VeryLongOrgNameSuchWow/ryzic/actions/workflows/codeql.yml)
[![Latest release](https://img.shields.io/github/v/release/VeryLongOrgNameSuchWow/ryzic?include_prereleases&sort=semver)](https://github.com/VeryLongOrgNameSuchWow/ryzic/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

A self-hostable Discord music bot. Plays YouTube audio in voice channels via [Lavalink](https://lavalink.dev/), with a local LRU cache so frequently-played tracks survive yt-dlp breakage. One bot per server you run yourself; no public hosting, no telemetry, no premium tier.

> **Roadmap:** see [open milestones](https://github.com/VeryLongOrgNameSuchWow/ryzic/milestones) and the [`epic` label](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues?q=is%3Aissue+is%3Aopen+label%3Aepic) for what's planned next.
>
> **Support:** maintainer is best-effort and typically responds to issues and PRs within ~a week. Bugs and feature requests go in [Issues](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues); see [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

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

See [Upgrading](#upgrading) for how the `:0.1` pin behaves on subsequent `docker compose pull`s.

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
| `RYZIC_AUTOLEAVE_SECONDS` | no | `300` | Seconds to wait after the queue empties before disconnecting from voice. `0` disables auto-leave entirely (24/7 ambient music deployments). |
| `RYZIC_YOUTUBE_COOKIES_PATH` | no | unset | Opt-in path to a YouTube `cookies.txt` (Netscape format). Unset = no cookies sent. **Read [Self-hoster considerations](#opt-in-youtube-cookies-for-age-restricted--private-content) before enabling.** |

## Upgrading

```bash
docker compose pull
docker compose up -d
```

`compose.yaml` pins the bot to the `:0.1` minor tag, so `docker compose pull` picks up 0.1.x patch releases automatically. Bumping to a future `:0.2` is a manual edit — pre-1.0 minor bumps may include breaking changes (see [SEMVER.md](SEMVER.md)). Pin to a specific `:vX.Y.Z` if you want full manual control.

The audio + playlist cache (the `RYZIC_CACHE_DIR` bind mount) and the SQLite database inside it persist across upgrades — `docker compose pull` only replaces the image, not the volume.

## Verifying the image (optional but recommended)

Each published GHCR image is signed with a [SLSA build provenance](https://slsa.dev/) attestation generated by GitHub Actions OIDC + Sigstore. To confirm the image you pulled was actually built by this repo's CI (and not a tampered fork or a registry compromise), resolve the digest of the tag you're running and verify it with the [`gh`](https://cli.github.com/) CLI:

```bash
docker buildx imagetools inspect ghcr.io/verylongorgnamesuchwow/ryzic:0.1
# copy the sha256:... from the Manifests: section, then:
gh attestation verify oci://ghcr.io/verylongorgnamesuchwow/ryzic@sha256:<digest> \
  --owner VeryLongOrgNameSuchWow
```

A successful verification proves the image was built by this repo's release workflow on a tagged commit and signed via Sigstore using a short-lived OIDC token issued at build time. It does **not** prove the source is bug-free or that any specific tag is what you expect — pin to a `:vX.Y.Z` tag and re-verify after each upgrade if you care about that.

## Self-hoster considerations

- **YouTube cookies are disabled by default.** Enabling them lets a deployment fetch age-restricted/private content under your account, which is a security and account-safety risk. The opt-in escape hatch is documented below — read it before flipping the env var.
- **The cache directory holds copyrighted material.** What you cache and how long you keep it is your responsibility. Default eviction is by least-recently-used at the `RYZIC_CACHE_MAX_GB` threshold.
- **No rate limiting in this release.** Anyone in your server who can run slash commands can fill your queue or your disk. If you don't trust everyone, restrict command access via Discord's built-in **Server Settings** → **Integrations** permissions before exposing the bot widely.
- **Single-instance only.** The audio cache, playlist cache, and per-guild state assume exactly one ryzic process per cache directory. Don't run two replicas against the same volume.

### Opt-in: YouTube cookies for age-restricted / private content

`RYZIC_YOUTUBE_COOKIES_PATH`. When set, ryzic uses the cookies-file at this path on every yt-dlp call.

**Read this before enabling**: anyone who can run a slash command on your bot can now fetch any video your YouTube account can see — including private uploads, age-restricted content, and YouTube Premium-only content. The cookies file effectively grants every `/play`-er a session as your account. Compromise of the bot host = compromise of your YouTube account. Do not enable on a server you don't fully control. If you suspect the bot host was ever compromised while cookies were active, sign out all sessions on the YouTube account (Google → Manage your Google Account → Security → Your devices) and re-export a fresh cookies file.

If — and only if — you've read the warning above and accept the tradeoff, the recipe below wires a host cookies file into the container.

#### 1. Extract cookies

Pick whichever path matches your environment. Use a YouTube account you'd be willing to lose; do not use your primary Google account.

- **Browser (recommended for desktop self-hosters).** Install the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) Chrome extension (open-source, runs locally — no server round-trip). Sign in to YouTube, click the extension, **Export As → youtube.com**, save as `youtube-cookies.txt` next to `compose.yaml`.
- **Headless host (no GUI).** With Chromium installed and signed in to YouTube, export from its cookie store via yt-dlp:

  ```bash
  uvx --with secretstorage yt-dlp \
    --cookies-from-browser chromium \
    --cookies youtube-cookies.txt \
    -o '%(id)s.skip' \
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
  ```

  The probe URL is a deliberately well-known public video; the `-o '%(id)s.skip'` template plus the implicit metadata fetch causes yt-dlp to write the cookies file without downloading any media. Re-run when the cookies expire.

#### 2. Set permissions (the easy-to-miss gotcha)

```bash
chmod 0644 youtube-cookies.txt
```

The container runs as UID 1001; your host file is owned by your UID (typically 1000). With mode `0600` the container can't read the bind mount and `/play` fails with `cannot read cookies` — the bot won't even reach yt-dlp. `0644` (other-readable) is what you want here. The file is bind-mounted `:ro`, so widening host-side read does not loosen container-side guarantees. Do not relax further (no `0666`); world-readable on the host serves no purpose for this recipe.

#### 3. Mount via compose override

Copy [`compose.override.yaml.example`](compose.override.yaml.example) to `compose.override.yaml` next to your `compose.yaml`:

```bash
curl -fsSLO https://raw.githubusercontent.com/VeryLongOrgNameSuchWow/ryzic/main/compose.override.yaml.example
mv compose.override.yaml.example compose.override.yaml
```

It mounts `./youtube-cookies.txt` read-only at `/etc/ryzic/youtube-cookies.txt` inside the container.

#### 4. Set the env var and bring it up

In `.env`:

```bash
RYZIC_YOUTUBE_COOKIES_PATH=/etc/ryzic/youtube-cookies.txt
```

Then, with both compose files:

```bash
docker compose -f compose.yaml -f compose.override.yaml up -d
```

ryzic copies the cookies file into its private cache directory at startup so YouTube's session-refresh writes stay contained — your source file at the bind-mount path is never modified. Re-run extraction (step 1) and `docker compose restart ryzic` when the cookies expire.

Unset `RYZIC_YOUTUBE_COOKIES_PATH` (the default) preserves the cookie-less behaviour described above.

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
