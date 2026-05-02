# CLAUDE.md

## Project

`ryzic` is a self-hostable Discord music bot. Stack: Python 3.13, hikari (gateway), hikari-lightbulb v3 (slash commands), lavalink.py (Devoxin client to a Lavalink Java audio server), yt-dlp, python-dotenv, aiosqlite. MIT-licensed. The intended deployment is `git clone` + `docker compose up -d` per self-hosted instance — not a published library.

The audio path: yt-dlp downloads to a local LRU cache, Lavalink plays from the local file via `LocalAudioSourceManager`. Per-guild queue. **No TTL on the audio cache** — it's deliberately "unsinkable when yt-dlp breaks between patches." Playlist metadata cache is live-first with a TTL fallback. Auto-leave 5min after queue ends.

## Commands

All run through `uv`:

- Run bot: `uv run python -m ryzic` (or `uv run ryzic`). Requires `DISCORD_BOT_TOKEN` + a reachable Lavalink (default `lavalink:2333` from `compose.yaml`).
- Tests (unit): `uv run pytest -q`
- Tests (integration; Docker required): `uv run pytest -q -m integration`
- Lint: `uv run ruff check`
- Format check: `uv run ruff format --check`
- Type check: `uv run ty check`
- Docker bring-up: `docker compose up -d` (after `cp .env.example .env` and pasting your token)

## Architecture

Entrypoint is `bot.py:main()`. It builds the hikari `GatewayBot`, the lightbulb v3 `Client`, opens the audio cache + sweeps orphans, registers commands, and wires lifecycle.

Module map (`src/ryzic/`):

- `bot.py` — entrypoint, lifecycle, command/extension load.
- `config.py` — env-var dataclass, fail-fast at startup.
- `lavalink_glue.py` — voice-update bridge, `EventHandler`, node bootstrap, 5-min auto-leave timer, per-guild state dicts, Discord endpoint allowlist.
- `audio_cache.py` — sqlite-backed LRU + per-video lock; `get_or_download` / `release` / `sweep_orphans`.
- `playlist_cache.py` — module functions for playlist metadata, live-first with 24h TTL fallback.
- `ytdlp.py` — yt-dlp wrapper, friendly error mapping per the UX spec.
- `url_validator.py` — `is_supported_url` (`urlparse` + hostname allowlist + https-only).
- `voice_check.py` — `ensure_same_voice` helper for voice-restricted commands.
- `ux.py` — embed builders, `escape_markdown`, `safe_truncate`, `format_duration`.
- `errors.py` — `FetchFailed`, `InvalidVideoID`.
- `commands/` — one slash command per file (lightbulb extension pattern).

Tests:

- `tests/test_*.py` — unit tests, fully mocked at module boundaries.
- `tests/integration/` — gated behind `@pytest.mark.integration`; default `pytest -q` skips them via `addopts = -m 'not integration'`.

## Project conventions

- **Conventional commits** mandatory (`feat:`, `fix:`, `docs:`, `chore:`, etc. — see `RELEASING.md` for the bump table).
- **release-please** owns versioning + `CHANGELOG.md`. See `RELEASING.md` for the flow.
- **PR workflow**: feature branch + PR; CI green required; per-PR `/review` + `/security-review` + `/simplify` review-agent reports get posted as PR comments (not committed files).
- **KISS / DRY / SRP / SOLID**. No premature abstractions. No comments narrating WHAT — only WHY when non-obvious.
- **No half-finished features**.
- **Plan → review plan → implement** for non-trivial work. Planning lives in GitHub Issues.
- **Pre-1.0 SemVer** — minor bumps may break. See `SEMVER.md`.
- **Never commit secrets**. `.env` is gitignored, broader credential patterns too. `.env.example` only.

## Env vars

Canonical reference is `.env.example` and the env-var table in `README.md`. Required: `DISCORD_BOT_TOKEN`. Everything else has a default.

## Lavalink + yt-dlp pinning

Lavalink server image is pinned to a `:4.x.y` minor in `compose.yaml`. `youtube-source` plugin version is pinned in `lavalink/application.yml`. yt-dlp ships frequent releases (YouTube changes); dependabot bumps it ungrouped (vs grouped `python-minor-and-patch` for everything else) so each YouTube fix gets reviewed individually.

## Cache directory

Mounted as a docker named volume at `/var/cache/ryzic` in BOTH the `ryzic` and `lavalink` containers (lavalink is `:ro`). If you change `RYZIC_CACHE_DIR`, change it in both services. The cache holds copyrighted audio files — that's the self-hoster's responsibility per `README.md`.
