# PR #4 Simplification Pass — `feat(deploy): Dockerfile + docker-compose + Lavalink config`

**Branch:** `feat/docker-compose` -> `main`
**Diff reviewed:** +548 LOC across 11 files (Dockerfile 43, .dockerignore 43, ci.yml +37, compose.yaml 34, application.yml 57, integration test 140, pyproject 7, plus uv.lock + fixtures).
**Companion docs:** `docs/plans/M1-simplify.md` (locked plan-level cuts), `docs/plans/M1.md` §9 (compose shape) + §10 (env vars) + PR7 entry in §12.

This pass looks only for shapes that are more elaborate than M1 §9–§10 justifies, and that the maintainer would reasonably want collapsed under "three similar lines is better than a premature abstraction." It does not critique correctness or security (other agents).

Honest framing: PR4 is **mostly tight** — the Dockerfile, compose stack, and `.dockerignore` are at or near the right size. The biggest real wins live in `application.yml` (one block is verbatim upstream defaults) and in the integration test's permissions stanza (over-engineered for a smoke test). Realistic floor of LOC saved: **~30–55 LOC**, none of which changes the surface contract or weakens the testcontainer's signal.

---

## Findings

### S-1 — Drop the explicit `clients:` list in `application.yml`

**What to cut/collapse:** Remove the `plugins.youtube.clients` block (lines 40–44, six lines):

```yaml
    clients:
      - MUSIC
      - ANDROID_VR
      - WEB
      - WEBEMBEDDED
```

**Where:** `lavalink/application.yml` lines 40–44.

**Why it's safe:** This list is the verbatim `DEFAULT_CLIENTS` array from `youtube-source` upstream (`common/src/.../YoutubeAudioSourceManager.java`):
```java
public static final Client[] DEFAULT_CLIENTS = new Client[] {
    new Music(), new AndroidVr(), new Web(), new WebEmbedded()
};
```
The plugin's `YoutubePluginLoader` falls back to `DEFAULT_CLIENTS` when `clients:` is unset. Restating the default in our config buys nothing now and pins us to the **v1.18.0 default** in perpetuity — meaning when upstream rotates clients (which they do, as YouTube breaks individual InnerTube clients), self-hosters get the stale list until they manually edit `application.yml`. Letting the default float is the better default for an OSS project that's tracking upstream's plugin minor.

The plan (§9) doesn't mention specific clients; this is an implementer choice, and "no opinion → take upstream's" is the right posture.

**Estimated LOC saved:** 6 lines (5 list entries + the `clients:` key).

---

### S-2 — Drop the verbose source toggles; rely on global `local: true` only

**What to cut/collapse:** In `lavalink/application.yml` lines 14–22, the eight explicit `false` source toggles:

```yaml
    sources:
      youtube: false
      bandcamp: false
      soundcloud: false
      twitch: false
      vimeo: false
      nico: false
      http: false
      local: true
```

Collapse to:
```yaml
    sources:
      youtube: false   # plugin replaces it
      local: true
```

**Where:** `lavalink/application.yml` lines 14–22.

**Why it's safe:** Lavalink's `application.yml` defaults already enable bandcamp / soundcloud / twitch / vimeo / nico / http — but the plan's threat-model in §3 / §4 is **YouTube only**, so the bot will never *send* loadtracks queries that match those source managers' patterns. `url_validator.py`'s allowlist (`youtube.com`, `youtu.be`, `m.youtube.com`, `music.youtube.com`) bounces them at the bot layer first. The defense-in-depth value of `bandcamp: false` etc. is real but small — and the plan doesn't ask for it. Per "three similar lines vs premature abstraction," six identical `false` lines for a constraint not in the spec is bloat.

If the maintainer *does* want belt-and-suspenders source restriction, a one-line comment `# only the URL validator and `youtube: false` are load-bearing; other source managers idle without input` documents the intent without 6 toggles.

**Estimated LOC saved:** 6 lines (could go to 7 if `youtube: false` retains its existing inline comment).

**Note:** This is the most arguable cut on the list — if the maintainer prefers explicit-deny for security posture, keep it. Listed because the PR doesn't reference any plan section that asks for these toggles, so it qualifies as "more elaborate than the spec justifies."

---

### S-3 — Replace the chmod stanza in the integration test with a single tmpdir mode

**What to cut/collapse:** `tests/integration/test_lavalink_smoke.py` lines 71–79:

```python
# Lavalink runs as uid 322 inside the container. Bind mounts must be
# readable (and the plugins dir writable) by that uid; pytest tmpdirs
# default to 0700, which silently yields a permission-denied container
# crash or an EMPTY load result.
for d in (config_dir, plugins_dir, cache_dir):
    d.chmod(0o755)
(config_dir / "application.yml").chmod(0o644)
(cache_dir / FIXTURE.name).chmod(0o644)
plugins_dir.chmod(0o777)  # Lavalink needs to drop the downloaded jar here.
```

Collapse to:
```python
# pytest tmpdirs are 0700; Lavalink (uid 322) needs world-read on dirs
# and write on plugins/ for its first-boot jar download.
for d in (config_dir, plugins_dir, cache_dir):
    d.chmod(0o755)
plugins_dir.chmod(0o777)
```

(File-level chmods are unnecessary — `shutil.copy` preserves source modes, and source files in the repo are already 0644.)

**Where:** `tests/integration/test_lavalink_smoke.py` lines 71–79.

**Why it's safe:** `shutil.copy` copies file mode along with data; the repo's `application.yml` and `silence.ogg` are already mode 0644 in the worktree. The redundant per-file chmods just guard against a hypothetical future where those files become 0600 in the repo — a problem nothing else in the project addresses or signals. Comment WHAT (line 79 narrates "Lavalink needs to drop the downloaded jar here") collapses into the consolidated WHY at the top.

**Estimated LOC saved:** 4–5 lines.

---

### S-4 — Drop `_load_with_retry` helper; let testcontainers' wait strategy do its job

**What to cut/collapse:** `tests/integration/test_lavalink_smoke.py`:
- Lines 17 (`import asyncio`), 19 (`import time`)
- Lines 26 (`from lavalink import ... LavalinkError ...` — only `LavalinkError` is used by the helper)
- Lines 119 (call site `result = await _load_with_retry(node, ...)`)
- Lines 129–140 (the entire `_load_with_retry` function body)

Replace with:
```python
result = await node.get_tracks(f"{CACHE_PATH_IN_CONTAINER}/{FIXTURE.name}")
```

**Where:** `tests/integration/test_lavalink_smoke.py` lines 17, 19, 26 (`LavalinkError`), 119, 129–140.

**Why it's safe:** The test already gates on `LogMessageWaitStrategy("Lavalink is ready to accept connections")` — that log line is emitted by Lavalink **after** `LavalinkServer` finishes binding to its REST port and source managers are loaded. The "loadtracks settles a moment after the readiness log line" claim in the helper's comment is speculative; if it really were true, the fix would be a *different* wait strategy (e.g. `HttpWaitStrategy("/version")`), not a per-test retry loop layered on top.

Worst case: the first `get_tracks` call hits a 1–2s window where Lavalink is "ready but warming," fails, and the test reports it directly via the assertion message. That's a clearer failure mode than "10s of silent retry then assertion." If we ever observe this happen in CI, swap the wait strategy in one place. Until then, 12 lines of speculative retry are net-negative.

**Estimated LOC saved:** ~14 lines (helper + imports + call-site indirection).

---

### S-5 — Drop the `:Z` suffix from integration-test volume mounts (or comment-only documentation)

**What to cut/collapse:** `tests/integration/test_lavalink_smoke.py` lines 81–94:

```python
# `:Z` is a no-op on Docker hosts without SELinux; on Podman + SELinux
# (Fedora) it relabels the mount so the in-container lavalink uid can read.
# First boot also fetches the youtube-source plugin from Maven (~30s cold).
container = (
    DockerContainer(LAVALINK_IMAGE)
    ...
    .with_volume_mapping(
        str(config_dir / "application.yml"), "/opt/Lavalink/application.yml", "ro,Z"
    )
    .with_volume_mapping(str(plugins_dir), "/opt/Lavalink/plugins", "rw,Z")
    .with_volume_mapping(str(cache_dir), CACHE_PATH_IN_CONTAINER, "ro,Z")
```

Collapse mode strings to the unsuffixed forms (`"ro"`, `"rw"`, `"ro"`).

**Where:** `tests/integration/test_lavalink_smoke.py` lines 91, 93, 94 (`,Z` suffix on three mode strings) and the 3-line comment block at 81–83.

**Why it's safe:** The companion `compose.yaml` (lines 23–25) does **not** use `:Z` for the same three mounts (`./lavalink/application.yml`, `./lavalink/plugins`, `cache:/var/cache/ryzic`). Either both should have it (consistency) or neither does. If the test passes locally on Maddie's Fedora VM today via the Docker-not-Podman path, then `:Z` was never actually exercised; if the suite is meant to run under rootless Podman + SELinux, `compose.yaml` is the one with the bug, not the test. Pick one rule:

- **Drop from test** (recommended): match compose, accept "Podman + SELinux requires `--security-opt label=disable` or running the test on a Docker host."
- **Add to compose**: more code, but consistent. Defer to PR9 (docs) — out of scope here.

This PR's job is to ship M1 deployment; SELinux portability of tests is not in M1 §9 acceptance criteria. Picking the test-drop variant for now is the smaller change.

**Estimated LOC saved:** 3 inline (`,Z` suffix on three lines) + the 3-line comment. Net ~6 lines, with bonus consistency between test and compose.

---

### S-6 — Inline `_probe_docker` body into the fixture, drop the function

**What to cut/collapse:** `tests/integration/test_lavalink_smoke.py` lines 38–51 + 56:

```python
def _probe_docker() -> bool:
    # The CLI binary is irrelevant — testcontainers talks to the daemon socket
    # directly (DOCKER_HOST or unix:///var/run/docker.sock by default), which
    # also happens to work with rootless Podman's compatible socket.
    try:
        from docker.errors import DockerException  # type: ignore[import-untyped]
        from testcontainers.core.docker_client import DockerClient
    except ImportError:
        return False
    try:
        DockerClient().client.ping()
    except DockerException:
        return False
    return True


@pytest.fixture(scope="module")
def lavalink_container(tmp_path_factory: pytest.TempPathFactory):
    if not _probe_docker():
        pytest.skip("Docker daemon not reachable")
```

Inline:
```python
@pytest.fixture(scope="module")
def lavalink_container(tmp_path_factory: pytest.TempPathFactory):
    try:
        from docker.errors import DockerException  # type: ignore[import-untyped]
        from testcontainers.core.docker_client import DockerClient
        DockerClient().client.ping()
    except (ImportError, DockerException):
        pytest.skip("Docker daemon not reachable")
```

**Where:** `tests/integration/test_lavalink_smoke.py` lines 38–51, 56.

**Why it's safe:** Single caller, single use. Function-extraction is justified when there are ≥2 callers or when the body is inscrutable enough to need a name. The probe body is ~3 lines of try/except; the surrounding 4-line WHY comment is the actual valuable part and stays. No reuse exists or is planned (the test is intentionally the only integration test in M1 per §12 PR7).

**Estimated LOC saved:** ~5 LOC (function def + return statement + the redundant try-block split).

---

### S-7 — Cut comments narrating WHAT in `application.yml`, `.dockerignore`, `compose.yaml`

Small, scattered. Examples:

- `lavalink/application.yml` line 7: `# Bundled YouTube source is deprecated in Lavalink v4 — use the dedicated plugin.` — keep (this one is genuine WHY).
- `.dockerignore` lines 30–31: `# Tests + docs aren't needed in the runtime image. README.md is intentionally / # kept — pyproject.toml references it and uv build reads it during sync.` — keep (real WHY: explains the absence of `README.md` from the ignore list).
- `.dockerignore` line 16: `# Python build artefacts` — narrates WHAT (the next 6 lines are obviously Python build artifacts). Cut.
- `.dockerignore` line 24: `# Local sqlite scratch` — narrates WHAT. Cut.
- `.dockerignore` line 35: `# Compose stack and lavalink config aren't part of the bot image` — borderline; this one is WHY-ish (explains why `compose.yaml` is in `.dockerignore` despite being a runtime-adjacent file). Keep.
- `.dockerignore` line 41: `# Editor + OS noise` — WHAT. Cut.

**Where:** `.dockerignore` lines 16, 24, 41.

**Why it's safe:** `__pycache__`, `*.sqlite`, and `.DS_Store` are self-documenting to anyone who works on Python projects. Comments that name the category without explaining the policy are skim-noise.

**Estimated LOC saved:** 3 LOC.

---

### S-8 — Inline `pytest.ini_options.markers` registration, drop the explanatory comment

**What to cut/collapse:** `pyproject.toml` lines 58–63:

```toml
# Integration tests (Docker required) are opt-in: `pytest -m integration` to run them,
# `pytest -q` (the default in CI's fast lane) skips them.
addopts = "-m 'not integration'"
markers = [
    "integration: tests that spin up real services (Docker required)",
]
```

The marker-self-description (`"integration: tests that spin up real services..."`) and the toml-level comment say the same thing twice. Trim the comment:

```toml
addopts = "-m 'not integration'"
markers = [
    "integration: tests that spin up real services (Docker required); run with `pytest -m integration`",
]
```

**Where:** `pyproject.toml` lines 58–59.

**Why it's safe:** Pytest prints the marker description on `--markers` and in `PytestUnknownMarkWarning`s — that's where someone discovers what `integration` means. The toml-level comment is invisible to that user; it's only seen by someone reading `pyproject.toml`, who can also read `addopts` directly. Single source of truth.

**Estimated LOC saved:** 2 LOC.

---

### S-9 — Things to keep as-is

These I considered cutting and decided not to.

#### Dockerfile two-stage build — **keep**.
Single-stage would either ship `uv` + the build cache in the runtime image (size penalty + attack surface) or cram everything into one `RUN` block to clean up after itself (less readable, no longer leverages BuildKit's cache mounts cleanly). The two stages here aren't a "premature abstraction" — they're the canonical uv-on-Debian pattern, and `astral-sh/uv-docker-example` ships exactly this shape. ~25 lines of `FROM build` is justified.

#### CI split into 3 jobs — **keep**.
- `lint-and-test` is the fast feedback lane.
- `docker-build` runs in parallel (no `needs:`) and is independent of Python — it gates "image still builds" without serializing on the test job. Merging it into `lint-and-test` would double the wall-clock for the fast lane.
- `integration` is correctly behind `needs: lint-and-test` (don't pay testcontainer cost when unit tests are red).

This is the right shape. It earned its split.

#### `.dockerignore` comprehensive list — **keep mostly**.
`.dockerignore`'s job is to be paranoid. `.git`, `.env*`, `.claude/`, `.venv`, `*.sqlite*` all genuinely belong; the secrets-related entries (`.env.*` with `!.env.example` re-include) are load-bearing. Only the WHAT-narrating headers in S-7 should go.

#### Healthcheck retry count / interval — **keep**.
`interval: 10s, timeout: 3s, retries: 5` is 50s of grace before compose declares the lavalink container unhealthy. First boot downloads `youtube-source` from Maven (~30s cold per the plan's README §10 step 7); 50s grace covers cold boot without leaving headroom for an actually-stuck Lavalink. If anything, this could be tightened, but the current shape is conservative-correct, not over-engineered.

#### `addopts = -m 'not integration'` + a separate `integration` marker — **keep**.
Yes, there's only one integration test today. But (a) the marker is the canonical pytest mechanism for opt-in slow tests, (b) PR descriptions reference it (`pytest -q` in fast lane / `pytest -m integration` for the integration job), and (c) the alternative — a `pytest.skip` inside the test based on env var — moves the gate from configuration to runtime, which is the worse shape. The marker earns its keep with one user.

#### `tmp_path_factory` + `shutil.copy` of `application.yml` into a tmpdir — **keep**.
The fixture copies the repo's `application.yml` into a tmpdir before mounting. The comment ("the repo file may live under restricted parent dirs in some sandboxes") is real — pytest CI runners and containerized dev shells sometimes have non-traversable parents. Worth the 5 lines.

#### Skipping when `FIXTURE` is missing — **keep**.
Fixture file is committed (`silence.ogg`), so the skip is defensive. But it's two lines and is the difference between a clean skip and a `FileNotFoundError` from `shutil.copy`. Cheap insurance.

#### No README/docs changes — **excellent, exactly right**.
PR9 in the M1 plan explicitly defers all README work until after PR7 lands. PR4 ships zero doc changes — no preview README sections to drift, no `compose up` instructions that PR9 will rewrite. This is the discipline the plan asked for.

---

## Top 3 simplification wins

1. **Drop `clients:` block in `application.yml` (S-1)** — 6 LOC, but the bigger win is *not pinning the bot to v1.18.0's default client list when upstream rotates them*. This is the single best signal-to-noise trim.
2. **Drop `_load_with_retry` and use the readiness wait strategy directly (S-4)** — 14 LOC, removes a speculative retry loop that masks the failure mode it pretends to handle.
3. **Collapse the chmod stanza (S-3) + drop the verbose source toggles (S-2)** — together ~10 LOC, and reduces "stuff that's there because the implementer wasn't sure" to "stuff that's there because it's needed."

## Report

**(a)** File saved at `/home/user/Projects/ryzic/docs/plans/PR4-simplify.md`.

**(b)** Top 3: see above.

**(c)** Total LOC the PR could lose: **~38–48 LOC** (S-1: 6, S-2: 6–7, S-3: 4–5, S-4: 14, S-5: 6, S-6: 5, S-7: 3, S-8: 2). No structural rewrites; all are local cuts that preserve PR4's surface contract (compose stack still boots, Dockerfile still produces the same runtime image, integration test still gates the same Lavalink ↔ local-file path).

**Honest verdict:** PR4 is *mostly* tight. The Dockerfile, compose stack, healthcheck, CI split, and pytest marker setup are all earned. The cuts are real but small — clustered in `application.yml` (verbatim upstream defaults) and the integration test (over-engineered permissions handling for a one-test smoke check). No "delete a whole subsystem" win; this is polish, not rework.
