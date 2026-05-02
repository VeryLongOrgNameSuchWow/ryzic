# PR #4 — Security Review

**PR**: `feat(deploy): Dockerfile + docker-compose + Lavalink config`
**Branch**: `feat/docker-compose` -> `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commit reviewed**: `6d5283f`
**Files in scope** (PR diff only):
- `Dockerfile` (new, 43 lines)
- `compose.yaml` (new, 34 lines)
- `lavalink/application.yml` (new, 57 lines)
- `lavalink/plugins/.gitkeep` (new, empty)
- `.dockerignore` (new, 43 lines)
- `.github/workflows/ci.yml` (added `integration` + `docker-build` jobs)
- `tests/integration/__init__.py` (new, empty)
- `tests/integration/test_lavalink_smoke.py` (new, 140 lines)
- `tests/integration/fixtures/silence.ogg` (new, 4526 bytes binary)
- `pyproject.toml` (added `testcontainers` dev dep + `integration` marker)
- `uv.lock` (resolution for `testcontainers` and its transitive closure)

This review covers the ten focus areas from the brief: Dockerfile hygiene, compose
exposure surface, Lavalink config, GHA workflow security, `.dockerignore`
completeness, integration-test isolation, fixture provenance, dependency CVE
scan, reproducibility, and self-hoster permission posture.

---

## Findings

### 1. Dockerfile — non-root user, layer hygiene, no baked secrets

**Severity**: (informational - no finding)
**What**: The runtime stage creates a fixed `ryzic` user (uid 1001, gid 1001), `mkdir -p /var/cache/ryzic` with `chown ryzic:ryzic`, `COPY --chown=ryzic:ryzic` for the staged venv, and finishes with `USER ryzic`. Login shell is `/usr/sbin/nologin`. Build stage does not declare any `ARG` (no build-time args that could leak via image history) and does not set any `ENV` containing default tokens or passwords - only `UV_*` knobs and `PATH/PYTHON*/RYZIC_CACHE_DIR`. The `COPY` lines copy `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, then `src/`. Combined with `.dockerignore` (see #5), `.env`, `.git`, `lavalink/`, `tests/`, `docs/` are excluded from the build context, so they cannot land in any layer. `--mount=type=cache,target=/root/.cache/uv` keeps the uv cache out of the final image. Final stage is `python:3.13-slim-bookworm` (no shell-on-`apt-add` calls; no `apt-get install` adds attack surface). Verified `--chown` is applied to the COPY-from-build, so the runtime user owns its venv. **Verdict: clean.**

---

### 2. Dockerfile reproducibility - base images pin to floating tags

**Severity**: LOW (M1 deploy posture; not exploitable by itself)
**What**: Three image references in the build are pinned by tag, not by `@sha256:` digest:
- `python:3.13-slim-bookworm` (build + runtime stages)
- `ghcr.io/astral-sh/uv:0.11.6` (uv binary)
- (related, see #4) `ghcr.io/lavalink-devs/lavalink:4.2.2` in `compose.yaml`
**Where**: `Dockerfile:4`, `Dockerfile:6`, `Dockerfile:26`; `compose.yaml:17`.
**Why it matters**: A tag is mutable. If any of those publishers were compromised (or an upstream registry hijack occurred), `docker pull` would fetch a substitute image transparently. uv is at least pinned to a patch version (`0.11.6`), but the publisher could republish that tag. Python's `:3.13-slim-bookworm` floats across patch releases - acceptable for a hobbyist self-hoster but not what a security-conscious deployer would want. The same concern applies to `4.2.2` Lavalink tag. The `docker-build` CI job rebuilds on every push, so this also means CI runs are not bit-reproducible: a quiet upstream change can break or compromise builds at any time without a corresponding diff.
**Why it's LOW, not MEDIUM**: M1 plan §9 explicitly accepts a tag-pinned posture for the first deploy iteration; there's no signed/notary pipeline to anchor digests against; and the Discord-bot threat model is "self-hoster runs this on a personal box," not "production multi-tenant service." Pinning by digest also means dependabot won't auto-bump security fixes - the project would need to either accept staleness or wire up renovate's `pinDigests + updatePinnedDependencies`. Deferring to a later iteration is the principled tradeoff.
**Fix**: Defer to follow-up issue. When pinning, swap to e.g. `python:3.13.0-slim-bookworm@sha256:<digest>` and `ghcr.io/astral-sh/uv:0.11.6@sha256:<digest>`. Pair with a renovate/dependabot rule that updates the digest alongside the tag. Add a TODO comment in `Dockerfile` at the affected lines so the next pass finds them.

---

### 3. compose.yaml - Lavalink not exposed on host, password handling correct

**Severity**: (informational - no finding)
**What**: The `lavalink` service in `compose.yaml` declares no `ports:` mapping, so Lavalink's port 2333 lives on the compose-default bridge network only and is unreachable from the host or LAN. Inter-service traffic uses the compose service DNS (`LAVALINK_HOST: lavalink`). The Lavalink container's listen address (`0.0.0.0:2333`) is fine because it's confined to the bridge network. Password defaults to `youshallnotpass` via the `${LAVALINK_PASSWORD:-youshallnotpass}` substitution in compose **and** in `application.yml` - because the network is not host-reachable, the default is acceptable for a home deploy. `cache:/var/cache/ryzic:ro` correctly enforces read-only on the Lavalink side (only `ryzic` writes; Lavalink reads). The `application.yml` mount uses `:ro`. The shared `cache` named volume is isolated to compose. `restart: unless-stopped` is appropriate; the bot's bad-credentials path (Discord 4004) raises and exits the process with no auto-recovery loop within the bot - compose will restart, the user will see the same crash, and they'll need to fix the token. Not an infinite-loop credential-spam risk against Discord because hikari backs off on auth failures and the restart cadence is capped by the start time of the container. **Verdict: clean.**

---

### 4. compose.yaml - `lavalink/plugins/` mount is read-write (by necessity), but that's the only attack surface a malicious plan-modifier has

**Severity**: LOW (defense-in-depth observation, not an exploitable bug)
**What**: `./lavalink/plugins:/opt/Lavalink/plugins` is mounted **without** `:ro`. Lavalink writes the downloaded `youtube-source` plugin jar there on first boot, so it must be writable. That's the correct tradeoff for an out-of-the-box experience - documented in M1 plan §10 and visible in the integration test (`plugins_dir.chmod(0o777)` for the same reason).
**Where**: `compose.yaml:24`.
**Why it matters**: A self-hoster who mistakenly drops a malicious `.jar` into `./lavalink/plugins/` on the host would have it executed inside the Lavalink container at next start. This is defense-in-depth at most: the attacker would already need shell on the host. No realistic attack vector unless someone misuses the directory as a download target. The README (PR9) should mention "do not put files in `lavalink/plugins/`; Lavalink manages it."
**Fix**: Optional. Either accept and document, or split: declare the youtube-source jar via `application.yml` only, mount `lavalink/plugins:` `:ro`, and on first boot bind a separate `plugins-cache:` named volume. The added complexity probably isn't worth it for M1; flag for PR9 README copy: "this directory is managed by Lavalink; do not put untrusted jars here."

---

### 5. lavalink/application.yml - password sourced from env, no hardcoded credentials, plugin source authentic

**Severity**: (informational - no finding)
**What**: `password: "${LAVALINK_SERVER_PASSWORD:youshallnotpass}"` reads from the env var Lavalink receives via compose. The fallback `youshallnotpass` matches `compose.yaml` and `.env.example` - consistent default, not a hardcoded secret. The `youtube-source` plugin dependency `dev.lavalink.youtube:youtube-plugin:1.18.0` resolves to the official `lavalink-devs/youtube-source` Maven artifact - same group/artifact published by `lavalink-devs` (the same org that publishes the Lavalink server image). All other source managers (`bandcamp`, `soundcloud`, `twitch`, `vimeo`, `nico`, `http`) are explicitly disabled, narrowing the attack surface to "audio that the bot intentionally cached" plus YouTube via the plugin. `server.sources.youtube: false` (deprecated bundled source) is correctly disabled per M1 plan §9. The dual-config of `server.sources.youtube: false` + `plugins.youtube.enabled: true` + `allowSearch: false`, `allowDirectVideoIds: false`, `allowDirectPlaylistIds: false` further restricts the YouTube plugin to "load only what the bot resolved to a URL," preventing arbitrary search/ID lookup from network-reachable callers. **Verdict: clean.**

---

### 6. GHA workflow - `permissions: contents: read` is correct floor, but third-party actions pin to major tags

**Severity**: LOW (supply-chain hardening gap)
**What**: `permissions: contents: read` is set at the workflow root - explicitly minimal. `concurrency` cancels stale runs. No `pull_request_target` is used (the `pull_request` trigger checks out the head SHA without secrets). `GITHUB_TOKEN` is implicit and limited to `contents: read`. `actions/checkout@v6`, `astral-sh/setup-uv@v8.1.0`, `docker/setup-buildx-action@v3`, and `docker/build-push-action@v6` are all referenced by tag. Of these, `setup-uv` is the only one pinned to a patch (`@v8.1.0`); `actions/checkout`, `docker/setup-buildx-action`, and `docker/build-push-action` use major-only tags that float. This is identical to the existing PR #1 posture and consistent with the rest of `.github/workflows/`.
**Where**: `.github/workflows/ci.yml:22, 25, 51, 54, 70, 71, 73`.
**Why it matters**: A compromised release of `actions/checkout@v6` (org owns the repo, so the threat model is supply-chain compromise of the action publisher itself - rare but documented; e.g. `tj-actions/changed-files` 2024) would leak our `GITHUB_TOKEN` (read-only here, so impact is small) and could plant code in the build context for the `docker-build` job (which sees the full repo). The minimal `permissions:` block keeps the blast radius small. Pinning to a full `@<commit-sha>` would close the residual risk, at the cost of every dependabot bump producing churn. Recommend deferring to a follow-up issue along with #2 (digest pinning); they're the same class of decision.
**Fix**: Defer to follow-up. When tightened, pin all third-party actions to commit SHAs and add a `dependabot.yml` group rule for "github-actions" that includes commit-sha bumps.

---

### 7. GHA workflow - `docker-build` job missing `permissions:` override (inherits the workflow-level `contents: read`, which is correct, but worth noting)

**Severity**: (informational - no finding)
**What**: The `docker-build` job correctly inherits `permissions: contents: read` from the workflow root (no per-job override). It does not push the image (`push: false, load: true, tags: ryzic:ci`), so no registry credential is required and `GITHUB_TOKEN` write is not needed. `cache-from: type=gha, cache-to: type=gha,mode=max` uses the GitHub Actions cache backend, which scopes to the workflow's repo and refs - no cross-repo leak path. The `docker-build` job runs on every PR including from forks, but because `pull_request` (not `pull_request_target`) is the trigger, the fork SHA is checked out without access to repo secrets. **Verdict: clean.**

---

### 8. .dockerignore - excludes the right things plus defense-in-depth credential patterns; one omission

**Severity**: LOW (consistency nit)
**What**: `.dockerignore` excludes `.env`, `.env.*` (with `!.env.example` override), `.git`, `.github`, `.venv`, tests, docs, compose/lavalink files, IDE noise. **Missing relative to `.gitignore`**: the credential-pattern hardening lines from `.gitignore` (`*.pem`, `*.key`, `*.crt`, `id_rsa*`, `id_ed25519*`, `secrets/`, `credentials.json`). These are unlikely to ever exist in the repo (they'd violate the project rule too) but `.dockerignore` should mirror `.gitignore`'s defense-in-depth posture so that if a self-hoster fork accidentally drops a key into the repo root and then runs `docker build`, the key never enters a layer.
**Where**: `.dockerignore:1-44` (compare `.gitignore:26-33`).
**Why it matters**: One of the top "I baked a secret into a Docker image" failure modes is a self-hoster dropping a `creds.json` next to their `compose.yaml` for some unrelated reason; `.dockerignore` is the last line of defense.
**Fix**: Append to `.dockerignore`:
```
# Generic credential patterns (defense-in-depth — never expected, never wanted)
*.pem
*.key
*.crt
id_rsa*
id_ed25519*
secrets/
credentials.json
```
One-liner, no behavior change for current repo state, mirrors `.gitignore`.

---

### 9. Integration test - no embedded Discord token, password is the same `youshallnotpass` default, image pinned by tag (mirrors compose.yaml)

**Severity**: (informational - no finding for token; tag-pin shared with #2)
**What**: `tests/integration/test_lavalink_smoke.py` does not import or reference any Discord credential; it constructs `lavalink.Client(user_id=1)` with a synthetic user_id and never connects to a real Discord. The Lavalink password literal `"youshallnotpass"` is the same harmless default used in `compose.yaml`/`application.yml` (i.e. the test is not betraying a production secret - the default exists publicly in the source). The image `ghcr.io/lavalink-devs/lavalink:4.2.2` is tag-pinned, not digest-pinned (same observation as #2; if you fix one, fix both). The fixture is mounted `:ro,Z` and the cache dir is mounted `:ro,Z` - the test can't write outside its tmpdir. SELinux relabel via `:Z` is a function-correctness fix, not a security risk. **Verdict: clean (modulo #2).**

---

### 10. OGG fixture - generated by ffmpeg/libopus, royalty-free

**Severity**: (informational - no finding)
**What**: `file tests/integration/fixtures/silence.ogg` reports `Ogg data, Opus audio, version 0.1, mono, 48000 Hz`. The OpusTags packet inside the file lists encoder strings `Lavf62.3.100` and `Lavc62.11.100 libopus` (FFmpeg's libavformat/libavcodec + libopus encoder). This is consistent with a one-shot synthesis like `ffmpeg -f lavfi -i anullsrc=r=48000:cl=mono -t 0.X -c:a libopus -b:a ... silence.ogg` rather than a re-encoded copyrighted source - the file is silence, only 4526 bytes, and contains no IRSC/title/album metadata. ffmpeg, libavformat, libavcodec, libopus are all under permissive/LGPL/BSD-style licenses; the encoded silence has no original creative content to copyright (Naruto Hiru notwithstanding - silence is not copyrightable). **Verdict: clean.**

---

### 11. uv.lock - new transitive deps audit

**Severity**: (informational - no finding)
**What**: This PR adds eight packages (one direct, seven transitive):

| Package | Version | Source | Notes |
| --- | --- | --- | --- |
| `testcontainers` | 4.14.2 | PyPI | Direct dev dep; current as of early 2026 |
| `docker` | 7.1.0 | PyPI | testcontainers transitive; current as of early 2026 |
| `requests` | 2.33.1 | PyPI | docker transitive; current as of early 2026 |
| `urllib3` | 2.6.3 | PyPI | requests transitive; CVE-2024-37891 fixed in 2.2.2 - we're well past |
| `certifi` | 2026.4.22 | PyPI | requests transitive; cert bundle, regularly refreshed |
| `charset-normalizer` | 3.4.7 | PyPI | requests transitive; pure Python, low risk |
| `pywin32` | 311 | PyPI | docker transitive; **only resolved on Windows** (env-marker gated, not installed on Linux/macOS hosts) |
| `wrapt` | 2.1.2 | PyPI | testcontainers transitive; widely used decorator helper |

All sources are `https://pypi.org/simple` (verified via `grep -E "^source = " uv.lock | sort -u` -> only PyPI and the editable project). No private indices, no Git URLs, no path-overrides. As of cutoff (Jan 2026), I'm not aware of an open critical/high CVE against the pinned versions of these packages. The dev-only scope (testcontainers is in `[dependency-groups].dev`, not `[project].dependencies`) means none of this ships in the runtime image - confirmed by `tests/` being in `.dockerignore` and `uv sync --no-dev` in the Dockerfile build stage. **Verdict: clean.**

---

### 12. Self-hoster permission posture (Administrator perm) - documented in M1 plan, README still pending

**Severity**: LOW (deferred to PR9, but flagged because the brief asks)
**What**: M1 plan §10 step 4 says: "Generate invite URL... permissions `Connect`, `Speak`, `Send Messages`, `Embed Links`, `Use Slash Commands`. **Do NOT use Administrator** - common rookie mistake; security smell in a music bot." The README is currently empty (PR9 is dedicated to docs). Nothing in the bot's runtime code can prevent a self-hoster from inviting the bot with Administrator - that's a Discord OAuth scope decision the deployer makes. No code path in `src/ryzic/` uses `MANAGE_GUILD`, `KICK_MEMBERS`, etc., so the bot doesn't *want* admin; the only exposure if a self-hoster ignores the doc is "if the Discord token leaks, the attacker has admin on every guild that invited the bot."
**Where**: This PR's diff doesn't touch README. The plan-level mitigation lives in `docs/plans/M1.md:430` and will land in PR9.
**Why it matters**: This is the canonical self-hoster footgun for Discord bots. The mitigation is doc-only - hard to enforce in code - so PR9 must call it out prominently. The bot DOES get one strong code-level mitigation right: `dm_enabled=False` at command registration (M1 plan §3) means commands can't be triggered in DMs even if the bot is in a guild as Administrator. Required intents are `GUILDS | GUILD_VOICE_STATES` - no `GUILD_MEMBERS`, `MESSAGE_CONTENT`, or `PRESENCES` - which limits what an admin-scoped token would expose.
**Fix**: Out of scope for this PR. Tracking note for PR9 reviewer: README "Setup" section MUST include the "Do NOT use Administrator" call-out + the recommended permission integer/scope set, and SHOULD include a "rotating your bot token" link to the Discord Developer Portal in case it leaks.

---

## Verdict

**fixes recommended**

No HIGH-severity findings. No merge blockers. Three LOW-severity items worth addressing:
- **#5 (.dockerignore credential patterns)** - one-line fix in this PR; mirrors `.gitignore`. Low effort, defense-in-depth win, recommend doing now.
- **#2 + #6 + #9 (digest pinning for base images and GHA actions)** - same class of decision; recommend a single follow-up issue tracking digest pinning + dependabot/renovate config to keep digests fresh.
- **#4 (lavalink/plugins writeable mount)** - documentation note in PR9; no code change needed.
- **#12 (Administrator perm warning)** - PR9 README must include the call-out.

The Dockerfile correctly drops to a non-root user with a deterministic uid, the compose stack confines Lavalink to a bridge network with no host port, the Lavalink config sources its password from env and disables every audio source manager except YouTube-via-plugin (with search/direct-id disabled), the GHA workflow has a minimal `permissions:` block and uses `pull_request` (not `pull_request_target`), the integration test embeds no real credentials and isolates volumes correctly, and the OGG fixture is a 4.4 KB ffmpeg-generated silence with no copyright concern. The transitive dependency closure is small (eight new packages, all from PyPI, all dev-only) and free of known critical CVEs at cutoff.

Apply the `.dockerignore` patch (#5) before merge if you want a fully clean review; otherwise the LOW items can ride into M2 follow-ups.
