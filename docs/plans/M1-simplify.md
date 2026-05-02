# M1 Plan — Simplification Pass

Reviewing `/home/user/Projects/ryzic/docs/plans/M1.md` exclusively for cuttable scope. Correctness/security is another agent's lane.

The plan is mostly tight. The cuts below are real, but small individually — there's no single "delete this whole subsystem" win. Total estimated LOC saved: **~250–400 LOC** (out of ~2150), plus 2–3 fewer PRs and 2 fewer config knobs.

---

## 1. Inline `ytdlp/` and `cache/` packages → flat modules

**What to cut/collapse:** Replace `ytdlp/{wrapper.py, models.py, __init__.py}` with a single `ytdlp.py`. Replace `cache/{audio.py, playlist.py, __init__.py}` with `audio_cache.py` + `playlist_cache.py` at top level.

**Where:** §2 module layout.

**Why it's safe:** Packages exist to group related modules when the count exceeds ~3 or when there's a real public/private split. Two files each is below that bar. `ytdlp/models.py` for two dataclasses (TrackInfo, PlaylistInfo) is the textbook case of premature splitting — they belong next to the only code that constructs them. The `__init__.py` re-exports become noise.

**Estimated LOC saved:** ~30 LOC (two `__init__.py` files + import indirection in tests/callers). Larger psychological win: one fewer "where does this go" decision per file added.

---

## 2. Drop `GuildStateRegistry`; use `dict[int, GuildState]` directly

**What to cut/collapse:** §8 defines a `GuildStateRegistry` class wrapping a single `dict[int, GuildState]` field with a `get(gid)` lazy-create method. Cut the class. Use `defaultdict(GuildState)` or `states.setdefault(gid, GuildState(gid))` at call sites.

**Where:** §8 per-guild state model.

**Why it's safe:** The registry has zero behavior beyond what `dict.setdefault` already provides. Wrapping it in a class is OOP ceremony that doesn't earn its keep — and `defaultdict` is the canonical Python idiom for this exact pattern.

**Estimated LOC saved:** ~15 LOC, but more importantly removes one type from the import graph.

---

## 3. Cut `RYZIC_PLAYLIST_CACHE_TTL_HOURS` env var

**What to cut/collapse:** Hardcode `PLAYLIST_TTL = timedelta(hours=24)` in the playlist cache module. Drop the env var, the config field, the `.env.example` line.

**Where:** §5, §10 env table.

**Why it's safe:** §5 is explicit that the TTL is **only used for the embed footer staleness warning** — the actual cache fallback fires on yt-dlp exception regardless of age. So this knob doesn't change cache behavior, only the "data is N hours old" cosmetic. No M1 user story justifies making that tunable. If someone asks, add it then; the change is one line.

**Estimated LOC saved:** ~5 LOC + one fewer env var to document, validate, and test.

---

## 4. Eliminate sidecar `.json` files; put metadata in `index.sqlite`

**What to cut/collapse:** §4 stores `{video_id}.json` next to each cached audio file containing `title, uploader, duration_ms, fetched_at`. Same data should live as columns on the `entries` sqlite row. Cut the JSON write path, the unlink-on-evict, the schema.

**Where:** §4 audio cache subsystem (storage layout + LRU section).

**Why it's safe:** Single source of truth: sqlite already exists for LRU bookkeeping; adding 4 more columns is `ALTER TABLE`-trivial. Two redundant write paths means two ways to get out of sync (e.g. evictor unlinks audio + json, then sqlite row deleted — if any step fails you have ghosts). Sqlite-only means one atomic delete.

The only argument *for* sidecars is human-readable inspection (`cat .cache/audio/dQ/.../foo.json`). For debugging, `sqlite3 index.sqlite "SELECT * FROM entries WHERE video_id = ?"` is one extra command.

**Estimated LOC saved:** ~40 LOC (sidecar serialization, write helper, dual-unlink in evictor, related tests).

---

## 5. Replace "shared lightbulb hook" with a 3-line helper

**What to cut/collapse:** §3 calls for "one shared lightbulb hook" enforcing same-voice-channel for `/skip`, `/pause`, `/resume`, `/leave`. Replace with `def require_same_voice(ctx) -> int` that raises a domain exception or responds + returns sentinel. Each of the 4 commands calls it as their first line.

**Where:** §3 cross-cutting check note (last line of §3).

**Why it's safe:** Hooks are framework indirection — registration order, error-routing, "where did this response come from when the test fails" mystery. A helper function is one import + one call. With only 4 callers, the WET cost is negligible (4 identical lines vs. the hook's registration + error-handler integration). Per the user's "three similar lines is better than a premature abstraction" rule.

**Estimated LOC saved:** Roughly net-zero LOC, but removes one framework concept. Could go either way; my call is helper.

---

## 6. Merge PR1 (skeleton) into PR2 (entrypoint)

**What to cut/collapse:** PR1's deliverable is a `__main__.py` that prints `"ryzic v0.0.1"`. That's not a meaningful end state to review independently. Merge PR1 into PR2 as one "skeleton + entrypoint + ping command" PR (~350 LOC, still under budget).

**Where:** §12 PR breakdown.

**Why it's safe:** Splitting PRs is for review-size and dependency clarity — not for ceremony. PR1 alone is unreviewable in any meaningful sense ("yep, the package imports"). Merging gives reviewer a working bot end-to-end.

**Estimated LOC saved:** ~0 LOC, but cuts one review cycle.

---

## 7. Dissolve PR8 ("polish") into PR2/PR6

**What to cut/collapse:** §12's PR8 contains: global lightbulb error handler, logging setup, removing the temporary `/lltest` command, smoke-test checklist. Logging belongs in PR2 (initial entrypoint, every dependency hits it from day one). The error handler + `/lltest` removal belong in PR6 (where commands land and the temporary one becomes obsolete in the same diff). Smoke-test checklist is a PR-description artifact, not its own PR.

**Where:** §12 PR breakdown.

**Why it's safe:** PR8 reads as "I'll clean it up later" — that's a smell. Land the logging at the same time as the bot it logs from; remove the temporary command in the same PR that makes it obsolete. No logical separation lost.

**Estimated LOC saved:** 0 LOC, but cuts another PR cycle and avoids the "polish PR" anti-pattern.

---

## 8. Drop pre-commit hook from acceptance criteria

**What to cut/collapse:** §11 #7 requires "pre-commit hook config committed". CI runs `ruff check` and `ty check` on every push (PR1 sets this up). Pre-commit is local-dev-experience for the maintainer; not load-bearing for "is M1 done".

**Where:** §11 acceptance criteria #7.

**Why it's safe:** CI gates the merge regardless. Pre-commit is opt-in convenience the user can install if she wants; doesn't need to be in the repo. If she does want it later, one-PR add.

**Estimated LOC saved:** ~30 LOC (pre-commit config, CONTRIBUTING note explaining how to install it).

---

## 9. Rephrase acceptance criterion #7 (lint passes)

**What to cut/collapse:** "ruff check . and ty check . both pass cleanly" is a CI gate, not a product-acceptance criterion. Cut from §11 or fold into a single "CI green" line.

**Where:** §11 acceptance criteria #7.

**Why it's safe:** Process vs product confusion. Other product criteria (commands behave per spec, eviction works, fallback works) are user-observable. "Lint passes" is invisible to the user and gated by CI.

**Estimated LOC saved:** 0 LOC; cleaner spec.

---

## 10. Demote `YtDlpService` and `PlaylistMetaCache` to module functions

**What to cut/collapse:**
- `YtDlpService(cache_root: Path)` with `resolve()` and `download()` → module-level `async def resolve(url, *, cache_root)` and `async def download(url, dest)`. The class has no internal state — `cache_root` is just a parameter.
- `PlaylistMetaCache` (implied from §5) → two functions: `read(playlist_id, cache_root) -> PlaylistInfo | None` and `write(playlist_id, cache_root, info) -> None`. No state.

Keep `AudioCache` as a class — it has real state (`_locks`, `_in_use`, sqlite connection).

**Where:** §6 (yt-dlp wrapper interface), §5 (playlist cache).

**Why it's safe:** A class with no fields beyond what could be a function param is just a namespace. Two functions are clearer at the call site (`from ryzic.ytdlp import resolve` vs `service = YtDlpService(cache_root); await service.resolve(url)`). Saves the construction-and-injection wiring through `lightbulb.di`.

**Estimated LOC saved:** ~20 LOC (constructor boilerplate, DI registration, mock setup in tests).

---

## 11. Cut "centralized `ux.py` for ALL strings" → only embed builders

**What to cut/collapse:** §3 says "All success and error strings live in `ux.py` … single source of truth". Recommend: `ux.py` holds **embed builders** only (`build_queued_track_embed(track, position)`, `build_queue_embed(now_playing, queue)`) — these have non-trivial logic and benefit from one home. Inline the one-shot strings (`"Nothing is playing."`, `"Paused."`, `"Already paused. Use /resume."`) directly in their command files.

**Where:** §3 (intro line) and §2 (file role).

**Why it's safe:** Centralization is justified when reuse > duplication or when the change-rate is high. Most of these strings appear in exactly one command and never change. Putting them in `ux.py` means `pause.py` does `respond(ux.PAUSE_NOT_PLAYING)` instead of `respond("Nothing is playing.")` — the latter is more readable at the point of use, and the former gives nothing back. Localization isn't an M1 concern.

**Estimated LOC saved:** Probably +20 LOC of constant declarations cut, no real LOC saving but better locality-of-reference.

---

## Things I considered cutting and decided not to

### `errors.py` (domain exceptions module)
**Keep.** `FetchFailed`, `InvalidVideoID`, `PlaylistFetchFailed` — three classes used across `ytdlp.py`, `audio_cache.py`, and `commands/play.py`. Centralizing them is correct; defining them in the file that raises them creates a circular-import risk when commands need to catch them.

### `commands/` as a package with one file per command
**Keep.** Lightbulb's `Loader` model loads extensions from a package, and one-file-per-command is the framework's natural grain. Collapsing to `commands.py` would require manual command registration. Net negative.

### `LocalAudioSourceManager` + dual-mount approach (§4, §9)
**Keep.** Considered: a Lavalink yt-dlp plugin (LavaSrc / lava-yt-dlp) that does the download inside Lavalink. Would eliminate the volume sharing entirely. But it defeats the user-controlled LRU cache (the plugin's cache is its own black box, not configurable to "survive yt-dlp breakage"). The cache requirement is load-bearing in the spec. Plan's approach is correct.

### Per-video locks with `_locks: dict[str, asyncio.Lock]` and ref-counted cleanup
**Keep.** Considered: `asyncio.Semaphore`-based or single-global-lock approach. But correctness here justifies the complexity — without per-video locks, two concurrent `/play` for the same video either downloads twice (the wasted-bandwidth problem the user explicitly cited) or serializes ALL downloads (latency hit). The dict-of-locks is the right shape.

### Sqlite for LRU bookkeeping (vs in-memory dict + persistence on shutdown)
**Keep.** Sqlite is already in stdlib; the "persist eviction order across restarts" requirement is real (cached files survive, so their access timestamps must too). In-memory + on-disk-pickle would be both more code and less robust.

### `RYZIC_GUILD_IDS` env var (instant-sync vs global commands)
**Keep.** This is the difference between "iterate in 1 second" and "wait an hour for global command propagation" during dev. Load-bearing for any self-hoster developing locally.

### Two listeners for the hikari↔lavalink.py voice-update bridge (§7)
**Keep.** Mandated by the libraries' shapes; nothing to simplify away.

---

## Total impact

- **LOC cut:** ~150–250 lines of code (sidecars, registry, service classes, ux registry boilerplate, pre-commit config).
- **LOC redistributed:** ~50–100 lines moved from PR1/PR8 into PR2/PR6.
- **Env vars cut:** 1 (`RYZIC_PLAYLIST_CACHE_TTL_HOURS`).
- **PRs cut:** 2 (PR1→PR2 merge, PR8 dissolved). 9 PRs → 7 PRs.
- **Subsystems removed:** sidecar JSON files, GuildStateRegistry class, central ux string registry, pre-commit hook setup.

---

## Top 3 simplification wins by impact

1. **Eliminate sidecar `.json` files; consolidate metadata into sqlite (#4)** — removes a whole second I/O path and a class of evictor sync bugs. ~40 LOC.
2. **Inline `ytdlp/` + `cache/` packages and demote `YtDlpService`/`PlaylistMetaCache` to functions (#1, #10)** — removes ~50 LOC of structure and DI wiring; flattens the import graph.
3. **Merge PR1→PR2, dissolve PR8 (#6, #7)** — 9 PRs → 7 PRs; first reviewable PR delivers a runnable bot, not a stub.

## Report

(a) File saved at `/home/user/Projects/ryzic/docs/plans/M1-simplify.md`.

(b) Top 3: see above.

(c) Total LOC the plan could lose: **~150–250 LOC of implementation + ~50 lines of plan/spec text + 2 PR cycles + 1 env var + 1 subsystem (sidecar metadata).** No structural rewrites; all are local cuts.
