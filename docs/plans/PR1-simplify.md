# PR #1 Simplification Pass — `feat: project skeleton, /ping bot, release-please, CI, dependabot`

**Branch:** `feat/skeleton-and-entrypoint` -> `main`
**Diff reviewed:** +1140 LOC across 16 files (~445 LOC excluding `uv.lock`).
**Companion docs:** `docs/plans/M1-simplify.md` (already-decided plan-level cuts), `docs/plans/PR1-review.md` (correctness review).

This pass looks only for code that's *more elaborate than its M1 surface justifies*. It does **not** re-litigate decisions already locked in `M1-simplify.md` (e.g. flat module layout, `errors.py` central placement, dropping pre-commit, dropping `RYZIC_PLAYLIST_CACHE_TTL_HOURS`).

Honest framing: this PR is mostly tight. Of the M1-simplify rules I tried to apply, only **two** found real fat. The rest of the file is a "keep as-is" recommendation.

---

## Findings

### S-1 — Drop `_format_uptime` and the `(uptime: ...)` suffix on `/ping`

**What to cut/collapse:** The `/ping` command body is `await ctx.respond(f"Pong (uptime: {uptime})")`. Replace with `await ctx.respond("Pong")`. Delete the `_format_uptime` helper and the `started_at: float` parameter that exists only to feed it. Delete the four `test_format_uptime_*` tests.

**Where:**
- `src/ryzic/bot.py:17-30` (`_format_uptime`)
- `src/ryzic/bot.py:34, 49, 65, 71` (`started_at` plumbing)
- `tests/test_imports.py:17-30` (four uptime tests)

**Why it's safe:** `/ping` is explicitly a smoke command that gets removed in PR6b (per `M1-simplify.md` §7 — "removes `/lltest` here, it's obsolete once `/play` exists" — same reasoning applies to `/ping`). The smoke goal is "did the bot register a slash command and respond" — `"Pong"` answers that perfectly. Adding uptime turns a smoke into a feature; nothing downstream needs it. Test scaffolding richer than the surface it tests (rule #9): four boundary-condition tests for a 14-line helper that lives one PR.

The `started_at = time.monotonic()` capture in `main()`, the parameter through `_build_client`, the closure capture inside the inner class, and the import of `time` all collapse with this.

**Estimated LOC saved:** ~30 LOC (14 src + 14 tests + import + plumbing).

---

### S-2 — Drop the `extra-files` block in `release-please-config.json`

**What to cut/collapse:** Remove the `"extra-files": [...]` array (lines 11-17) entirely.

**Where:** `release-please-config.json:11-17`.

**Why it's safe:** `"release-type": "python"` already updates `[project].version` in `pyproject.toml` natively — that's the single thing the explicit updater is configured to update. Configuration knobs not earned by use cases (rule #3): this is duplicating built-in behavior. Already flagged independently in `PR1-review.md` LOW-1 from the correctness side; from the simplification side it's the same cut for the same reason.

**Estimated LOC saved:** ~7 LOC (the `extra-files` array body).

---

### S-3 — Optional: collapse `_build_client` into `main`

**What to cut/collapse:** Inline the `_build_client(...)` body into `main()` directly. Or, if S-1 lands and the `Ping` command is also at module scope (it would no longer need `started_at`), `_build_client` reduces to two lines and inlines naturally.

**Where:** `src/ryzic/bot.py:33-52, 71`.

**Why it's safe:** `_build_client` is called exactly once, takes three params, and returns one value — it's a function-shaped paragraph break, not an abstraction. After S-1 lands the body is essentially `client = lightbulb.client_from_app(bot, default_enabled_guilds=cfg.guild_ids); ...register Ping...; return client` and the helper costs more than it saves. Modules that could be inlined / functions used in one place (rule #2 applied to function granularity).

I'm calling this **optional** because `main()` post-inline still reads cleanly at ~25 lines, and pulling out the `Ping` registration to module scope (the lightbulb-idiomatic shape per `PR1-review.md` LOW-5) is a separate refactor that should land together. If S-1 doesn't land, leave `_build_client` as-is.

**Estimated LOC saved:** ~5 LOC (signature, return, blank lines).

---

### S-4 — Optional: collapse the CI Python matrix

**What to cut/collapse:** Replace `strategy: matrix: python-version: ["3.13"]` and `${{ matrix.python-version }}` interpolation with a literal `3.13`.

**Where:** `.github/workflows/ci.yml:18-20, 29-30`.

**Why it's safe:** A 1-element matrix is matrix scaffolding without matrix value. `requires-python = ">=3.13"` and `.python-version` already pin the version single-source; CI doesn't gain anything from the matrix shape until a second version exists. Premature abstraction (rule #5) — "for future X" where X is "we add 3.14 later," and adding the matrix back is a one-PR change at that point.

I'm calling this **optional** because (a) the bloat is genuinely tiny — ~4 LOC, (b) some teams prefer to keep matrix scaffolding so version sweeps are mechanical, and (c) leaving it doesn't create any active maintenance burden. Leans cut, defensible to keep.

**Estimated LOC saved:** ~4 LOC.

---

## Things I considered cutting and decided not to

### `errors.py` (`FetchFailed`, `InvalidVideoID`)
**Keep.** No callers in this PR — looks textbook "for future X" (rule #5). But `M1-simplify.md` §"Things I considered cutting" already weighed this and kept it for a real reason: cross-module catches between `commands/play.py` and `ytdlp.py` would create a circular-import path if the exceptions lived next to either raiser. Re-cutting now would mean immediately re-introducing the file in PR3 with no net win. 14 LOC of upfront discipline is the right trade.

### `config.py`'s validation rigor (`_require`, `_parse_int`, `_parse_guild_ids`)
**Keep.** This *looks* like defensive code at internal boundaries (rule #6) but env vars are a system edge — they cross from the OS into the process. Validate-at-the-edge is the textbook KISS choice; the alternative is `os.environ["DISCORD_BOT_TOKEN"]` raising `KeyError` from inside a slash-command handler an hour into a session. The five validation tests (`test_config_*`) earn their keep — they pin the fail-fast contract.

### `config.py` module docstring (5 lines)
**Keep.** It explains *why* validation happens at startup (process-level fail-fast vs. handler-level surprise) — exactly the WHY-not-WHAT comment style the project standards endorse.

### `_parse_int` helper (used twice)
**Keep.** Two callers + the rule "three similar lines is better than a premature abstraction" technically suggests inlining. But each call site would then duplicate the try/except + ConfigError construction (~5 lines each), so inlining grows the file. The helper pays for itself on 2 callers because the duplicated block is non-trivial.

### `lightbulb` `Ping` class defined inside `_build_client`
**Keep for this PR.** `PR1-review.md` LOW-5 already covered this and called it "gold-plating a command that's about to be deleted." Same conclusion here from the simplification side: the class disappears in PR6b along with `/ping`, so promoting it to module scope plus `client.di` injection would add machinery for a temporary command. If S-1 lands, `started_at` goes away and the closure motivation evaporates — but the class-inside-function shape is still defensible for a one-PR-lifespan command.

### `__main__.py` (3 lines)
**Keep.** `python -m ryzic` is a stable contract; the alternative is "`uv run ryzic` only" which loses the standard module-runner UX. 3 lines is the minimum.

### `dependabot.yml` config (27 lines)
**Keep.** Two ecosystems (pip, github-actions), one grouping rule with a single `yt-dlp` exclusion — every line is doing work. The inline comment explaining *why* `yt-dlp` is ungrouped (frequent YouTube-fix releases) is exactly the WHY-comment standard. No bloat.

### `.env.example` (25 lines, 6 documented vars)
**Keep.** Every var documented matches a field in `Config`. Comments distinguish required vs. optional and explain the compose-vs-local override case. No commented-out stub vars, no "future X" placeholders.

### `pyproject.toml` ruff rule selection (`E,W,F,I,B,UP,SIM,RUF`)
**Keep.** Eight rule families is on the lean end of "useful default set" — none of these are exotic. No selection-explosion smell.

### `release-please.yml` workflow (19 lines)
**Keep** (after S-2 cuts the `extra-files` block in the *config*). The workflow itself is the minimum viable release-please setup: trigger, perms, action call. Nothing to trim.

### Multiple separate CI steps for ruff-check / ruff-format / ty / pytest
**Keep.** `PR1-review.md` already noted this is good practice — separate steps mean failures are diagnosable from the GitHub Actions UI without scrolling logs. Combining them into one `make ci` would save 4 lines of YAML at the cost of debuggability.

### `.gitignore` (24 lines)
**Keep.** Every block is justified (Python build artifacts, venv, `.env` with the "NEVER commit" comment per project memory, `.cache/`, sqlite ancillaries, the `.claude/` worktree dir). The agent-worktree comment is the WHY type. No bloat.

### `tests/__init__.py` (empty)
**Keep.** Empty marker for pytest discovery — that's its whole job. Nothing to cut.

---

## Top 3 wins

1. **Drop `_format_uptime` + uptime-suffix on `/ping` (S-1)** — ~30 LOC. Largest single cut; removes test scaffolding richer than its surface, removes a feature not earned by the smoke use-case, simplifies the `/ping` registration path.
2. **Drop `extra-files` in `release-please-config.json` (S-2)** — ~7 LOC. Pure dead config; release-type:python already does this. Same finding as PR1-review LOW-1, viewed from the simplification axis.
3. **Optional inline of `_build_client` into `main` (S-3)** — ~5 LOC. Function used in exactly one place; particularly natural after S-1 lands. Defensible to skip.

## Total LOC the PR could lose

- **Solid wins (S-1 + S-2): ~37 LOC** (out of ~445 hand-written, or ~8%).
- **Including optional cuts (S-1 + S-2 + S-3 + S-4): ~46 LOC.**

This is a tight PR. The remaining ~400 LOC are doing real work — config validation tests, CI workflow, dependabot config, and the lightbulb skeleton are all at or near minimum viable.
