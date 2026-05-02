# PR #1 — Security Review

**PR**: `feat: project skeleton, /ping bot, release-please, CI, dependabot`
**Branch**: `feat/skeleton-and-entrypoint` → `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commits reviewed**: `feb5a84`, `b70808c`, `0004bf3`, `d7bc4dc`, `9e71aa9` (and `.gitignore` carried in from `0104cdc`).

Scope: secrets handling, `.gitignore`, CI workflow security, dependency security, logging hygiene, `pyproject.toml` / `uv.lock`, repository-level config, release-please permissions.

---

## Findings

### 1. No committed secrets — clean

**Severity**: (informational — no finding)
**What**: Every tracked file under the PR was scanned for credential strings (`token`, `secret`, `password`, common API-key prefixes, Discord token regex `[A-Za-z0-9_-]{24,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}`). Nothing real surfaced.
**Where**: All tracked files; specifically:
- `.env.example` — `DISCORD_BOT_TOKEN=` is empty (placeholder); `LAVALINK_PASSWORD=youshallnotpass` is the public upstream Lavalink default, not a secret.
- `tests/test_imports.py` — uses `"fake-token"` literal as monkeypatched value.
- `src/ryzic/config.py` — defaults `LAVALINK_PASSWORD` to `"youshallnotpass"` (Lavalink upstream default).
- `src/ryzic/bot.py` — only uses `cfg.discord_bot_token` as a hikari arg; never logs it.
- `.github/workflows/release-please.yml` — uses `${{ secrets.GITHUB_TOKEN }}`, the GHA-provided per-job token, not a static value.
- No `.env` in git history (`git log --all -- .env` is empty).

Verdict: clean.

---

### 2. CI workflow action pinning — appropriate

**Severity**: LOW (informational; current pinning is acceptable per stated policy)
**What**: All three actions used are from trusted publishers (per the audit policy "tolerable for trusted publishers (actions/*, astral-sh/*); third-party should be SHA-pinned"). No third-party actions.
**Where**:
- `.github/workflows/ci.yml:22` — `actions/checkout@v6` (floating major, GitHub-owned, trusted)
- `.github/workflows/ci.yml:25` — `astral-sh/setup-uv@v8.1.0` (exact tag, Astral, trusted; deliberately tighter than necessary per commit `9e71aa9`)
- `.github/workflows/release-please.yml:15` — `googleapis/release-please-action@v5` (floating major, Google-owned, trusted)
**Why it matters**: Floating tags allow a compromised maintainer or repo to push a malicious release that gets picked up on the next workflow run. Tradeoff: SHA pinning gives stronger supply-chain guarantees but loses automatic security patches and creates dependabot churn for trivial bumps.
**Fix**: No action required for M1. If you ever want maximum hardening later, run `gh api repos/{owner}/{repo}/git/ref/tags/{tag} --jq .object.sha` and pin to commit SHAs (with the readable tag in a `# v6.x` trailing comment). Dependabot already covers `github-actions` weekly so SHA bumps would be automated.

---

### 3. CI workflow `permissions:` block — correct (least-privilege)

**Severity**: (informational — no finding)
**What**: `ci.yml` declares `permissions: contents: read` at the top level, narrowing the default `GITHUB_TOKEN` scope from the org/repo default (which can be write) down to just read. The release workflow declares `contents: write` + `pull-requests: write`, exactly what release-please needs and nothing more.
**Where**: `.github/workflows/ci.yml:8-9`, `.github/workflows/release-please.yml:7-9`.
Verdict: clean.

---

### 4. `pull_request_target` / untrusted code execution

**Severity**: (informational — no finding)
**What**: Audited for any workflow triggered by `pull_request_target` that checks out fork code. None present. CI uses plain `pull_request` (which checks out the PR head in an unprivileged context with the read-only `contents: read` token).
Verdict: clean.

---

### 5. release-please `GITHUB_TOKEN` scoping — correct

**Severity**: (informational — no finding)
**What**: Per `googleapis/release-please-action` docs, the action requires `contents: write` (to create release branches, tags, and GitHub Releases) and `pull-requests: write` (to open the release PR). The workflow grants exactly those two — nothing extra (no `actions: write`, no `id-token: write`, no `packages: write`, etc.). Token is the per-job ephemeral `GITHUB_TOKEN`, not a long-lived PAT or App token.
**Where**: `.github/workflows/release-please.yml:7-9, 19`.
Verdict: minimum viable scope. Clean.

---

### 6. `.gitignore` — hardening opportunity

**Severity**: LOW
**What**: `.gitignore` covers the must-haves (`.env`, `.cache/`) and project-specific artifacts (sqlite WAL/SHM/journal, `.venv`, `.claude/` worktrees). It does NOT preemptively exclude common credential file patterns: `*.pem`, `*.key`, `*.crt`, `*.p12`, `id_rsa*`, `id_ed25519*`, `.envrc`, `secrets/`, `*.kdbx`, `*.gpg`. Given the recent near-miss on `.env`, broadening this is cheap insurance against the next foot-gun.
**Where**: `/home/user/Projects/ryzic/.gitignore` (carried in from initial commit `0104cdc`, unchanged in this PR).
**Why it matters**: A contributor running `gh auth setup-git`, generating a key for testing, or dropping a service-account JSON into the repo root would have nothing protecting them. Broad patterns cost nothing and harden against the entire class of mistake.
**Fix**: Append to `.gitignore`:

```gitignore
# Generic credential / key files — defense-in-depth
*.pem
*.key
*.crt
*.p12
*.pfx
id_rsa
id_rsa.pub
id_ed25519
id_ed25519.pub
.envrc
.env.*
!.env.example
secrets/
*.kdbx
*.gpg
```

The `!.env.example` exception keeps the placeholder file tracked. (Consider opening as a follow-up issue rather than blocking this PR — `.env` and `.cache/` are already covered, which were the specifically-required entries.)

---

### 7. `Config` dataclass has no `__repr__` redaction

**Severity**: LOW
**What**: `config.Config` is a frozen dataclass holding `discord_bot_token: str` and `lavalink_password: str`. Default dataclass `__repr__` will print these in plaintext. If any future code does `_log.debug("loaded %s", cfg)` or `repr(cfg)` (e.g. in an exception handler, debug REPL, or third-party error reporter), the bot token leaks into logs.
**Where**: `/home/user/Projects/ryzic/src/ryzic/config.py:18-27`.
**Why it matters**: The point of failing fast on startup is to keep secrets in memory and never on the wire. A redacted `__repr__` is a one-liner that closes off an entire class of accidental leak. Right now `bot.py` doesn't log the cfg object, but PR3a/PR4 implementers may add `_log.exception` blocks or use `Sentry`/structlog and accidentally serialize it.
**Fix**: Either (a) override `__repr__` to mask the two sensitive fields, or (b) use `dataclasses.field(repr=False)` for `discord_bot_token` and `lavalink_password`. Option (b) is one line per field and is the idiomatic fix:

```python
@dataclass(frozen=True)
class Config:
    discord_bot_token: str = field(repr=False)
    ...
    lavalink_password: str = field(repr=False)
    ...
```

(Will need `from dataclasses import dataclass, field`.) This is hardening, not a present-day bug — flag as a follow-up rather than a merge blocker.

---

### 8. Dependency security — no known CVEs at versions resolved

**Severity**: (informational — no finding)
**What**: Resolved versions in `uv.lock` (cross-checked vs cutoff Jan 2026):
- `aiohttp 3.13.5` — well past CVE-2024-42367 (path traversal, fixed 3.10.5) and CVE-2024-52303 (memory leak, fixed 3.10.11).
- `yt-dlp 2026.3.17` — current rolling release per the `>=2026.3.17` floor.
- `python-dotenv 1.2.2`, `aiosqlite 0.22.1`, `hikari 2.5.0`, `hikari-lightbulb 3.2.4`, `lavalink 5.11.0` — all current minor/patch lines, no outstanding advisories I'm aware of.
- Dev: `pytest 9.0.3`, `pytest-asyncio 1.3.0`, `ruff 0.15.12`, `ty 0.0.34` — fine.
- Transitive `confspec 0.0.5` and `linkd 0.3.0` are tandemdude's (lightbulb maintainer) own utility crates pulled by hikari-lightbulb; expected.

All packages source from `https://pypi.org/simple` (verified by `grep "^source = " uv.lock` — every entry uses the PyPI registry, no Git URLs, no path overrides, no custom indices).
Verdict: clean.

---

### 9. Logging hygiene — config-loading path safe

**Severity**: (informational — no finding)
**What**: Only one log line in the codebase: `_log.info("ryzic starting; log level=%s", cfg.log_level)` at `src/ryzic/bot.py:63`. `cfg.log_level` is a non-secret string. No `print(cfg)`, no `_log.debug(cfg)`, no exception handler that re-raises with the env contents. `dotenv.load_dotenv()` itself does not log file contents.
**Where**: `/home/user/Projects/ryzic/src/ryzic/bot.py:63`.
Verdict: clean. (See finding #7 for the forward-looking risk.)

---

### 10. `pyproject.toml` and `uv.lock` — no sketchy sources

**Severity**: (informational — no finding)
**What**: `pyproject.toml` declares all deps via PEP 508 specifiers with no extras pulling from non-PyPI indices, no `[tool.uv.sources]` overrides, no `[[tool.uv.index]]` pointing anywhere besides PyPI default. `uv.lock` `revision = 3` confirms lockfile integrity is enforced. `--frozen` flag in CI (`uv sync --frozen`) ensures no implicit upgrades during build.
**Where**: `/home/user/Projects/ryzic/pyproject.toml`, `/home/user/Projects/ryzic/uv.lock`, `.github/workflows/ci.yml:33`.
Verdict: clean.

---

### 11. Repository-level: CODEOWNERS, branch protection, dependabot

**Severity**: LOW (CODEOWNERS — out of scope per PR plan; branch protection — gated on free-tier upgrade)
**What**:
- No `.github/CODEOWNERS` file. For a single-maintainer FOSS project this is fine; once external contributors arrive a CODEOWNERS pointing every path at `@riohno` would auto-tag review requests.
- Branch protection on `main` cannot be configured: repo is private + free tier (`gh api repos/.../branches/main/protection` returns 403 "Upgrade to GitHub Pro or make this repository public"). The PR description explicitly notes "we'll add CI-required-green after this lands" — tracked, in-scope-for-later.
- `.github/dependabot.yml` is well-formed: `pip` weekly with minor/patch grouping (excluding `yt-dlp` so its frequent releases each get their own PR, deliberate per the comment); `github-actions` weekly. `open-pull-requests-limit: 10` is sensible. Both ecosystems labeled appropriately. No secret-bearing config.
**Where**: `/home/user/Projects/ryzic/.github/dependabot.yml`.
**Fix**: None blocking. Once the repo goes public (or upgrades to Pro), enable branch protection: require PR review, require CI green, require `feat/`/`chore/`/`fix/` linear history if you care, dismiss stale approvals on push. Consider a one-line CODEOWNERS (`* @riohno`) before the first external contributor.

---

## Summary table

| # | Finding | Severity |
|---|---|---|
| 1 | No committed secrets | clean |
| 2 | CI action pinning (trusted publishers, mixed pin styles) | LOW (informational) |
| 3 | CI `permissions:` blocks (least-privilege) | clean |
| 4 | No `pull_request_target` exposure | clean |
| 5 | release-please `GITHUB_TOKEN` scope (minimum viable) | clean |
| 6 | `.gitignore` lacks generic credential patterns | LOW |
| 7 | `Config` dataclass has no `__repr__` redaction | LOW |
| 8 | No known CVEs in resolved dep versions | clean |
| 9 | Logging hygiene (only safe log line) | clean |
| 10 | `pyproject.toml`/`uv.lock` from PyPI only | clean |
| 11 | No CODEOWNERS / branch protection (gated externally) | LOW |

**HIGH-severity findings: 0**
**MEDIUM-severity findings: 0**
**LOW-severity findings: 3** (`.gitignore` hardening, `Config.__repr__` redaction, repo-level governance)

---

## Security verdict

**clean** — no merge-blocking issues.

The three LOW-severity items are hardening opportunities worth opening as follow-up issues, not gates on this PR:
- `.gitignore` broadening (defense-in-depth against the next near-miss; lift after merge as a one-line PR)
- `Config` `__repr__` redaction (closes off accidental token leak via future debug logging; do before PR4 lands real secrets-in-flight)
- CODEOWNERS + branch-protection follow-up (already noted in the PR description)

Foundation PR is genuinely solid on the supply-chain and secrets axes — least-privilege workflow tokens, PyPI-only dep registry, no committed creds, lockfile-frozen CI, no `pull_request_target` foot-guns.
