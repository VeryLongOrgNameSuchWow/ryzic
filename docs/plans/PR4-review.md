# PR #4 Review — `feat(deploy): Dockerfile + docker-compose + Lavalink config`

**Branch:** `feat/docker-compose` → `main`
**Scope per plan:** `docs/plans/M1.md` §9 (compose shape), §10 (env vars), §12 PR7 (Dockerfile + compose + lavalink config + integration test).
**Diff:** 11 files changed, +548 / -1. Dockerfile (43), `compose.yaml` (34), `lavalink/application.yml` (57), `tests/integration/test_lavalink_smoke.py` (140), `.dockerignore` (43), CI delta (+36), `pyproject.toml` (+7), `lavalink/plugins/.gitkeep` (0), `tests/integration/__init__.py` (0), `tests/integration/fixtures/silence.ogg` (4526 B), `uv.lock` (+188).
**External version checks:** Lavalink `4.2.2` is the latest tag (released 2026-03-06) — pin is current. youtube-source `1.18.0` is the latest plugin release (2026-02-23) — pin is current. Lavalink's documented Maven coordinate for v4 is `dev.lavalink.youtube:youtube-plugin:VERSION` with `snapshot: false`, which the PR uses verbatim. `defaultPluginRepository` (`https://maven.lavalink.dev/releases`) is the right host for that artifact, so no `repository` key is needed. Confirmed via the upstream README at the `1.18.0` tag.
**Local verification:** `lavalink.py` 5.11.0 exposes `Client`, `LavalinkError`, `LoadType.TRACK`, `Node.get_tracks(query)`, `Client.add_node(..., connect=False)`, async `Client.close()` — all the surface the integration test relies on. Lavalink ready-log line ("Lavalink is ready to accept connections.") matches the wait-strategy substring (verified via `gh search code` on `lavalink-devs/Lavalink:LavalinkServer/src/main/java/lavalink/server/Launcher.kt`). Lavalink container ships `wget` at `/usr/bin/wget` (verified via `podman run`), so the compose healthcheck's `CMD wget …` works on the upstream image.
**Container-mount sanity:** Reproduced the bind-mount permissions issue described in MEDIUM-1 below by hand against `ghcr.io/lavalink-devs/lavalink:4.2.2`.

---

## Findings

### MEDIUM-1 — `lavalink/plugins/` host bind-mount is owned by the cloning user; Lavalink (uid 322) cannot write the downloaded plugin jar on first boot

- **Severity:** MEDIUM
- **Where:** `compose.yaml:24` (`./lavalink/plugins:/opt/Lavalink/plugins`) + `lavalink/plugins/.gitkeep`
- **Why it matters:** The official Lavalink image runs as `lavalink:lavalink` uid/gid `322:322` (`LavalinkServer/docker/Dockerfile`@4.2.2 confirms it). On first boot the container writes the resolved `youtube-plugin-1.18.0.jar` into `/opt/Lavalink/plugins/`. With the current PR, that path is a bind-mount from `./lavalink/plugins/` in the cloned repo — owned by whichever uid did the `git clone` (typically `1000:1000`) with mode `755`. Reproduced locally:
  ```
  $ podman run --rm --userns=keep-id:uid=322,gid=322 \
      -v /tmp/host_plugins:/opt/Lavalink/plugins:rw \
      ghcr.io/lavalink-devs/lavalink:4.2.2 \
      touch /opt/Lavalink/plugins/test.txt
  touch: cannot touch '/opt/Lavalink/plugins/test.txt': Permission denied
  ```
  The Lavalink service will crash on plugin resolution, the healthcheck will never go green, ryzic's `depends_on.lavalink.condition: service_healthy` will block forever, and `docker compose up` exits with a confused error chain. Plan §10 step 7 even promises "First boot pulls Lavalink image and downloads `youtube-source` plugin (~30s)" — so the README will lie. The integration test sidesteps this by `chmod 0o777` on its tmpdir plugins dir (`test_lavalink_smoke.py:79`), which is exactly the workaround that has to live in `compose.yaml` to keep the contract honest. Upstream Lavalink docs explicitly call out this footgun ("make sure to create the folder & set the correct permissions and user/group id mentioned above"). The PR neither documents nor automates around it.
- **Fix:** Pick one:
  1. **Use a Docker named volume for plugins** (preferred for this PR's KISS bias):
     ```yaml
     volumes:
       - ./lavalink/application.yml:/opt/Lavalink/application.yml:ro
       - lavalink_plugins:/opt/Lavalink/plugins
       - cache:/var/cache/ryzic:ro
     volumes:
       cache:
       lavalink_plugins:
     ```
     Compose creates the named volume root-owned and Lavalink's image is allowed to write inside it. Cost: the plugin re-downloads if the user runs `docker compose down -v` — acceptable (~30s, one-time).
  2. **Keep the bind mount but pre-set perms in repo** — `chmod 0777 lavalink/plugins/` and document it. Loses on `git clone` (umask resets) and is ugly.
  3. **Bake the plugin into a custom Lavalink image** — overkill for M1; defer to a follow-up if pre-pulled images become valuable.

  Drop the `lavalink/plugins/.gitkeep` placeholder if you go with option 1.

### MEDIUM-2 — `lavalink/application.yml` is bind-mounted host-readable but `LAVALINK_SERVER_PASSWORD` substitution requires the env var to be set inside the lavalink container (it is — but only because of compose env wiring; the YAML's literal default contradicts compose's default)

- **Severity:** MEDIUM
- **Where:** `lavalink/application.yml:13` (`password: "${LAVALINK_SERVER_PASSWORD:youshallnotpass}"`) vs `compose.yaml:21` (`LAVALINK_SERVER_PASSWORD: ${LAVALINK_PASSWORD:-youshallnotpass}`)
- **Why it matters:** Two different "default password" code paths, both saying `youshallnotpass`. That's harmless today, but they can drift. More importantly, the compose env mapping reads `LAVALINK_PASSWORD` from the host (via `.env`) and exports it to the container as `LAVALINK_SERVER_PASSWORD`. The bot side (per plan §10) reads `LAVALINK_PASSWORD`. So a self-hoster who sets `LAVALINK_PASSWORD=…` in `.env` gets a matched bot↔Lavalink password — that's correct. But the YAML's `${LAVALINK_SERVER_PASSWORD:youshallnotpass}` default exists only as a safety net; if compose's env wiring is ever simplified (or someone copies just the YAML to a non-compose deployment), they'll silently get `youshallnotpass` — the M1 plan's "weakest default in the repo" footgun. Worth one comment line in `application.yml` saying "compose passes this; literal default is the dev/test fallback only". Not a blocker, but documents the contract.
- **Fix:** Add a one-line comment above the password key explaining the indirection. Or, more aggressively, set `password: "${LAVALINK_SERVER_PASSWORD}"` (no default) so the dev/test fallback can't silently kick in if compose wiring breaks. Pick one — the current state is "two defaults, no comment".

### LOW-1 — `_JAVA_OPTIONS: "-Xmx512m"` is an "unsupported, may go away" knob; prefer `JAVA_TOOL_OPTIONS` or a JVM `-Xmx` arg directly

- **Severity:** LOW
- **Where:** `compose.yaml:19`
- **Why it matters:** `_JAVA_OPTIONS` is an internal HotSpot debug envvar — Oracle's docs label it "unsupported", IBM JDK ignores it, and Lavalink's own docs use `_JAVA_OPTIONS` in examples but `JAVA_OPTS` is the more common Lavalink-blessed knob. Plan §9 specified `_JAVA_OPTIONS` verbatim, so the PR is faithful, but the eclipse-temurin image (Lavalink's base) honors `JAVA_TOOL_OPTIONS` officially. Either works today; this is a forward-compat note, not a defect.
- **Fix:** Optional. If you change anything, switch to `JAVA_TOOL_OPTIONS: "-Xmx512m"` and update plan §9 in the same commit so the contract stays single-source.

### LOW-2 — CI `docker-build` job has no top-level cache permission; GHA cache works only via injected runtime token

- **Severity:** LOW
- **Where:** `.github/workflows/ci.yml:8-9` and `.github/workflows/ci.yml:67-81`
- **Why it matters:** Top-level `permissions: contents: read`. `cache-to: type=gha` writes to GitHub Actions cache. `docker/build-push-action@v6` reads `ACTIONS_RUNTIME_TOKEN` from the runner env (auto-injected) so this works in practice — but if GitHub ever tightens that path or you migrate to a self-hosted runner that strips it, the cache silently no-ops. Worth being explicit:
  ```yaml
  docker-build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: write   # for type=gha cache
  ```
- **Fix:** Add the per-job permission block. Two lines, prevents future surprise.

### LOW-3 — `_load_with_retry` only catches `LavalinkError`; aiohttp/asyncio connection errors aren't retried during the readiness window

- **Severity:** LOW
- **Where:** `tests/integration/test_lavalink_smoke.py:129-140`
- **Why it matters:** `Node.get_tracks` raises `RequestError` (≤ `LavalinkError`) for HTTP non-200, but `aiohttp.ClientError` and `asyncio.TimeoutError` for connection-level failures — neither subclasses `LavalinkError` (verified locally). The `LogMessageWaitStrategy` returns once "ready to accept connections" prints, but the Spring REST endpoint settles a beat later, so the first call is still the most likely to hit a connection-refused. With the current `except LavalinkError`, that first connection error escapes the loop and fails the test instead of retrying.
- **Fix:** Broaden the catch:
  ```python
  except (LavalinkError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
  ```
  Need `import aiohttp` (already a transitive of lavalink.py). Defensive against CI flake.

### LOW-4 — `lavalink_container` fixture pins `tmp_path_factory` mode 0755 / 0777 and chmods `application.yml` 0644 — the "permissions guard" comment is correct but the chmod chain doesn't survive `pytest --basetemp` to a 0700 parent

- **Severity:** LOW
- **Where:** `tests/integration/test_lavalink_smoke.py:75-79`
- **Why it matters:** The fixture chmods the *contents* of `tmp_path_factory.mktemp(...)` to 0755, but pytest's tmp root (`/tmp/pytest-of-<user>/pytest-<n>/`) defaults to 0700 ownership. On standard rootless setups, in-container uid 322 can't traverse a 0700 ancestor regardless of leaf perms. Reproducible if pytest's tmp root inherits a restrictive parent — common on CI runners that override `TMPDIR`. The comment at line 71-74 acknowledges this is the failure mode but the fix only chmods the leaf, not the chain. In practice GitHub runners give world-traversable temps, so this is latent — but flagging in case CI moves.
- **Fix:** Either (a) `os.chmod` the parent chain up to a known-traversable root, (b) bind-mount via a stage dir under `cache_dir` itself so only one chmod path exists, or (c) note it in a comment instead of the current "0700 yields …" line which describes the symptom but not the cause.

### LOW-5 — Dockerfile's two-stage `uv sync` runs the resolver twice (once with `--no-install-project --no-dev`, once with `--no-editable --no-dev`); cache layers are correct but the second invocation re-walks the lockfile

- **Severity:** LOW
- **Where:** `Dockerfile:17-22`
- **Why it matters:** The split is the canonical uv pattern (deps layer cached separately from source) and works as intended — but `uv sync --frozen` against a populated venv is a no-op for already-installed wheels, and the `--no-editable` flag on the second pass installs only the project itself non-editably. So the cost is one `uv` resolver pass for a one-package install, ~150ms. Not worth changing unless you trim further; mentioned only because the implementer report flagged "image 197 MB" as a metric and someone might wonder if the two-step costs space (it doesn't — both write to `/opt/ryzic`).
- **Fix:** None recommended. Consider whether `UV_CACHE_DIR` benefit (the `--mount=type=cache` block) is real on second invocation; it should be, for the project wheel build.

### LOW-6 — `.dockerignore` excludes `.python-version` but build relies on `python:3.13-slim-bookworm` base image; if you ever add `.python-version` for local pinning, ensure base image stays in sync

- **Severity:** LOW
- **Where:** `.dockerignore:6`, `Dockerfile:4,26`
- **Why it matters:** No `.python-version` in repo today, so this is a pre-emptive note. The Dockerfile hardcodes 3.13. If a future dev adds `.python-version` for `uv python install` convenience locally and bumps it (e.g. 3.14), the Docker image will silently keep building 3.13. Worth a comment in the Dockerfile near the `FROM` line: "If you change Python version here, also update `pyproject.toml`'s `requires-python` and the CI matrix."
- **Fix:** Optional, near-zero ROI.

### LOW-7 — `compose.yaml` doesn't pin `version:` — fine on Compose v2 spec, but emit one if you want to be explicit

- **Severity:** LOW (informational)
- **Where:** `compose.yaml:1`
- **Why it matters:** Compose v2 deprecated the `version:` key — omitting it is now correct. Just confirming the absence is intentional, not an oversight. (`docker-compose-v1` users still hit this path; M1's audience is `docker compose` v2+, so fine.)
- **Fix:** None.

### NOTE — Comment hygiene aligns with maintainer standards

- The Dockerfile has one-line "why" comments per stage (`# Build stage:`, `# Runtime stage:`). The integration test's docstring explains what the boundary test guards (the deploy-stack contract that only fails at the container boundary) and inline comments explain the SELinux `:Z` trick, the Lavalink uid 322 footgun, and the retry rationale — all "why" not "what". `application.yml` has one comment block calling out the deprecated bundled YouTube source. Good fit for the project standard.

### NOTE — `silence.ogg` provenance is clean

- 4526 bytes, mono Opus at 48 kHz, ~1 second. `ogginfo` shows `Vendor: Lavf62.3.100`, `encoder=Lavc62.11.100 libopus`, no other metadata. This is a synthetic ffmpeg-encoded silence (no third-party-derived audio), so it's de facto royalty-free — no licensing concern. The PR doesn't ship a README in the fixtures dir documenting how it was generated; would be nice to add a short comment in `tests/integration/fixtures/README.md` (one line: "ffmpeg -f lavfi -i anullsrc=cl=mono:r=48000 -t 1 -c:a libopus silence.ogg"). Optional.

### NOTE — Future-incident watchlist

- **Multi-arch image:** The `docker-build` CI job uses default `linux/amd64` only. The Lavalink image ships `linux/amd64,linux/arm/v7,linux/arm64/v8`, but ryzic's image is amd64-only. Any self-hoster running on Raspberry Pi / Apple Silicon Linux VM / Ampere Cloud needs to rebuild locally. Worth a follow-up issue: add `platforms: linux/amd64,linux/arm64` to `docker/build-push-action` and verify yt-dlp / lavalink-py wheels exist for arm64.
- **uv pin drift:** `Dockerfile:6` pins `ghcr.io/astral-sh/uv:0.11.6`. `pyproject.toml` requires `uv_build>=0.11.6,<0.12.0`. Latest uv is 0.11.8 (5 days fresher). Renovate/dependabot for the `COPY --from=ghcr.io/astral-sh/uv:X` line specifically would catch the drift; standard GH Dependabot supports this via the `docker` ecosystem on Dockerfiles. Add to a follow-up dependabot config.
- **Lavalink major-version pin protection:** `compose.yaml` pins `4.2.2`. Plan §9 watchlist already calls this out. When 4.3.x or 5.x lands, the integration test (`LAVALINK_IMAGE = "ghcr.io/lavalink-devs/lavalink:4.2.2"`) is the second place that hardcodes the version — the duplication is fine for a deploy PR but a bump touches three files (compose.yaml + test + the implementer-report comment in the commit message). Would be cleaner with a single `LAVALINK_IMAGE` constant referenced from both, but that's overkill for a two-file duplication.
- **youtube-source plugin lifecycle:** Plugin 1.18.0 already removed `TVHTML5EMBEDDED` (we're using `MUSIC, ANDROID_VR, WEB, WEBEMBEDDED` — none of which are the removed one, good). Future client removals are SemVer-exempt per the youtube-source versioning policy ("clients are not removed unless there is good reason"). The current client list is conservative; flag if a future plugin upgrade silently drops one of these four.
- **PID 1 signal handling:** The Dockerfile uses `ENTRYPOINT ["python", "-m", "ryzic"]` directly — Python is PID 1. `hikari.GatewayBot.run()` installs SIGINT/SIGTERM handlers explicitly, so `docker stop` should propagate cleanly. If a future refactor changes the entry path, watch for shutdown hangs and consider `tini` as init.
- **`.env` in compose context:** `compose.yaml:7` uses `env_file: .env` for the bot but compose itself also auto-loads `.env` for variable substitution (`${LAVALINK_PASSWORD:-…}`). Two layers, both reading the same file — harmless, just confusing if a self-hoster moves variables between the two scopes. Worth one line in the README's setup section.
- **Compose's `lavalink/plugins/` empty bind mount:** see MEDIUM-1. Once fixed, also consider whether `cache:/var/cache/ryzic:ro` on the lavalink side needs a `delegated`/`Z` flag for SELinux self-hosters. The bind variant already does in the integration test; the named-volume variant in compose doesn't need `:Z`.

---

## Verdict

**minor revisions.**

The scope is tight, the multi-stage Dockerfile is clean (cache-friendly COPY layers, non-root uid 1001 set up correctly with `--home-dir` + `--shell /usr/sbin/nologin`, ENTRYPOINT runs `python -m ryzic` matching `__main__.py`), and the compose shape matches plan §9 nearly verbatim with the appropriate Lavalink 4.2.2 bump. The `application.yml` correctly disables every non-local source per the plan, declares `youtube-plugin:1.18.0` with the right `dev.lavalink.youtube` Maven coordinates and Lavalink-v4 `snapshot: false` shape, and selects a sensible client tuple (`MUSIC, ANDROID_VR, WEB, WEBEMBEDDED`) — none of which are the deprecated `TVHTML5EMBEDDED`. The integration test idiomatically uses `testcontainers-python`'s `LogMessageWaitStrategy`, scopes the container to a module fixture (cleanup via `try/finally`), and explicitly handles the SELinux+rootless-Podman case with `:Z` mounts and chmod 0o644/0o755/0o777 on staged files. CI split is sound: `lint-and-test` → `integration` (sequential, post-lint), `docker-build` parallel with GHA layer cache. `pyproject.toml` adds `testcontainers>=4.14.2` to dev deps and `addopts = "-m 'not integration'"` keeps the unit lane fast — the right call.

The blocker-ish item is **MEDIUM-1**: the `lavalink/plugins/` host bind-mount will cause first-boot to fail for any self-hoster whose host uid isn't 322, which is essentially all of them. Reproduced locally. The cleanest fix is to swap to a named volume; it preserves restart-survival of the downloaded plugin and removes the empty `.gitkeep` artifact. **MEDIUM-2** is a five-minute clarity fix on the `application.yml` password indirection. The LOWs are nice-to-haves; **LOW-2** (`actions: write` on docker-build) and **LOW-3** (broaden the retry's exception net) are worth the two-line patches as defensive hygiene.

Once MEDIUM-1 is addressed, ship.
