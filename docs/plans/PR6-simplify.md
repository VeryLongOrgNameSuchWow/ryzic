# PR #6 Simplification Pass — `feat(cache): audio cache (sqlite-LRU + per-video lock)`

**Branch:** `feat/audio-cache` -> `main`
**Diff reviewed:** +956 LOC across 2 files (`src/ryzic/audio_cache.py` 362 LOC, `tests/test_audio_cache.py` 594 LOC).
**Companion docs:** `docs/plans/M1-simplify.md` (locked plan-level cuts), `docs/plans/M1.md` §4 (audio cache spec) + §12 (PR3b boundary), prior `PR3-simplify.md` / `PR4-simplify.md` / `PR5-simplify.md` for tone calibration.

This pass looks only for shapes that are more elaborate than M1 §4 justifies, under "three similar lines is better than a premature abstraction." It does **not** re-litigate decisions in `M1-simplify.md` (no sidecars, sqlite WAL, per-video locks deliberately leaked, `_in_use` Counter, orphan sweep with mtime guard, `release_many` for guild cleanup is in §4 spec). It does not critique correctness or security — those are other agents' lanes.

Honest framing: PR6 is **mostly tight**. The core class (eviction loop, double-checked locking, atomic replace, fast lookup, sweep) is right-sized for a security-sensitive subsystem. The wins are clustered in two places: (1) `_detect_ext` is M1's biggest "do we need this?" question — the magic-byte sniff exists for a debugging aesthetic, not a runtime constraint, and `4 codecs × 6 LOC` of branches collapses if the answer is "no"; (2) the test file leans on hand-rolled scaffolding for things `pytest.parametrize` would express in half the lines. Realistic floor: **~40–80 LOC of source + ~50–80 LOC of tests**, ~10–15% of the PR. No structural rewrites; surface contract unchanged.

---

## Findings

### S-1 — Drop `_detect_ext`; use a single `"audio"` extension everywhere

**What to cut/collapse:** Delete `_detect_ext` (lines 53–73, 21 lines including docstring) and its callsite in `_download_locked` (line 199). Replace with the constant `_FALLBACK_EXT` directly:

```python
# In _download_locked, around line 199:
final = _audio_path(self._cache_root, track.video_id, _FALLBACK_EXT)
```

Drop the per-codec branches; drop `_M4A_HEAD` / `_OPUS_HEAD` / `_WEBM_HEAD` magic byte constants from the test file (lines 28–30); drop the `test_detect_ext_recognizes_known_codecs` parameterized test (lines 8 cases) and `test_detect_ext_handles_unreadable_file` (lines ~470–490 in the test file).

The PR description's own justification answers itself:

> Lavalink's `LocalAudioSourceManager` sniffs the file body, so the on-disk suffix is decorative; we still want a consistent value for the sqlite `ext` column and easier debugging.

**Where:** `src/ryzic/audio_cache.py` lines 39–44 (`_FALLBACK_EXT` declaration stays, comment trims), lines 53–73 (`_detect_ext` definition), line 199 (one call). `tests/test_audio_cache.py` lines 28–30, the entire `_detect_ext` test section (~30 LOC), and the `expected: "m4a"` field assertion in `test_get_or_download_miss_writes_file_and_row` (line 535 — change to `"audio"`).

**Why safe:** The PR's own docstring concedes the on-disk suffix is **decorative** — Lavalink reads the file body to determine codec, and nothing in the bot's code path branches on `entries.ext`. The "easier debugging" argument is real but weak: the rare debugger case can `file <path>` once. The cost is 21 LOC of source + 4 magic-byte constants + 30 LOC of tests + a hidden coupling between `_detect_ext`'s branch coverage and `_fake_download`'s payload selection across the test suite (every eviction test had to use `_M4A_HEAD` to make `_detect_ext` choose the right ext, despite the tests not caring about ext at all).

This is the single biggest "earned vs unearned complexity" call in the PR. The code reads well, but it's solving a problem that doesn't exist downstream. Per `M1-simplify.md` §10's "do less until someone asks" posture: when a future contributor needs the ext for debugging or cross-tool inspection, add it back as one branch + one test. Until then, this is yt-dlp-format-introspection that the spec didn't ask for.

If the maintainer wants to keep *some* signal: keep `_FALLBACK_EXT = "audio"` as the column value, drop the sniffer.

**Estimated LOC saved:** ~21 LOC src + ~30 LOC tests = **~50 LOC**, plus removes 3 magic-byte constants from the test fixture surface area.

**Counter-argument considered and rejected:** "If a future PR adds a download-then-stream-to-Discord direct path, knowing the codec matters." That PR can also add the sniff in 8 lines. Building infrastructure for hypothetical future work is exactly what the M1 plan rule forbids.

---

### S-2 — `release_many` is a 2-line for-loop; let callers loop

**What to cut/collapse:** `release_many` (lines 253–260, 8 lines including docstring) is:

```python
async def release_many(self, video_ids: Iterable[str]) -> None:
    for vid in video_ids:
        await self.release(vid)
```

Drop it; let `lavalink_glue.py` (PR5/PR6a future work) write:

```python
for vid in queue_video_ids:
    await audio_cache.release(vid)
```

**Where:** `src/ryzic/audio_cache.py` lines 253–260. `tests/test_audio_cache.py` `test_release_many_decrements_each` (lines ~210–225, ~16 LOC) — drop entirely; the underlying `release` is already fully covered by `test_release_decrements_and_pops_at_zero` and `test_release_idempotent_after_zero`.

**Why safe:** "Three similar lines vs premature abstraction." Currently zero callers (PR3b is cache-only; the consumers are future PRs). The helper exists *speculatively* per the PR description's "decisions worth flagging":

> The per-guild cleanup hook stays guild-agnostic — cache iterates the `video_ids` it is given.

— but the cache iterating vs the caller iterating is the same loop, in either spelling. A 2-line for-loop at the call site is more readable than `await cache.release_many(queue)` and saves the reader one indirection ("what does `release_many` do? oh, exactly what it sounds like"). The helper buys nothing the caller doesn't already have.

Counter-argument: "What if `release_many` ever needs to do something more than the loop (batch-flush sqlite, single-pass `_in_use` mutation)?" — the cache currently has no batch-flush primitive; `release` is a memory-only mutation, so there's nothing to batch. If batching becomes necessary, the helper reappears with a non-trivial body and *earns* the name then.

**Estimated LOC saved:** ~8 LOC src + ~16 LOC tests = **~24 LOC**.

---

### S-3 — Inline `sweep_orphans`'s separate connection; document why module-level

**What to cut/collapse:** Nothing big. The module-level `sweep_orphans` placement is correct (M1 §4 specifies it's "on startup" and the PR description correctly notes "AudioCache.open() does NOT call sweep_orphans itself"); the separate aiosqlite connection is also correct because the cache may not yet be open at sweep time (or may be opened in a different lifecycle stage). **Keep the module-level shape.**

But the in-function comment (lines 336–338) over-explains:

```python
# Open a separate short-lived connection: the sweep is invoked
# at startup independently of :class:`AudioCache`, and SQLite
# WAL allows concurrent readers without contention.
```

The "independently of AudioCache" part is the genuine WHY (placement justification). The "WAL allows concurrent readers" half restates a property the module docstring already implies and that's not load-bearing here — sweep is one-shot at startup, no concurrency to enable. Trim to:

```python
# Sweep runs at startup independently of any AudioCache instance;
# open our own short-lived connection.
```

**Where:** `src/ryzic/audio_cache.py` lines 336–338.

**Estimated LOC saved:** ~1–2 LOC. **Low conviction; cosmetic.**

**Counter-considered cut and rejected:** "Make `sweep_orphans` a `@staticmethod` or `@classmethod` on `AudioCache` for namespace cohesion." The PR3b spec language ("on startup, walk audio/ directory…") doesn't bind it to the class instance, and the module-level shape lets a future CLI / cron invoke `python -c "from ryzic.audio_cache import sweep_orphans; ..."` without instantiating the cache. **Keep module-level.** Per `M1-simplify.md` §10's "demote classes to functions when they have no state" — the same logic argues for keeping a stateless function stateless.

---

### S-4 — `_fast_lookup` is read-only, but the wrapping is heavier than it needs to be

**What to cut/collapse:** `_fast_lookup` (lines 262–280, 19 LOC including docstring) is the right shape — read-only, no mutation, returns `Path | None`. The 7-line docstring (lines 263–269) is real WHY (explains why we don't repair from this branch), so it stays.

But the function's *interaction pattern* is the smell: every consumer (lines 161, 168, 182) does the same dance:

```python
hit = await self._fast_lookup(track.video_id)
if hit is not None:
    await self._touch(track.video_id)
    self._in_use[track.video_id] += 1
    return hit
```

This 4-line block appears verbatim **three times** in `get_or_download` (top-level, double-checked-locking re-check, and once on hit-after-lock). At three call sites, the "hit hookup" is the right unit to extract — not the `_fast_lookup` itself, but the *hit-finalization*:

```python
async def _finalize_hit(self, video_id: str, path: Path) -> Path:
    """On a confirmed cache hit: touch LRU + pin + return."""
    await self._touch(video_id)
    self._in_use[video_id] += 1
    return path
```

Then `get_or_download` becomes:

```python
async def get_or_download(self, track: TrackInfo) -> Path:
    validate_video_id(track.video_id)
    hit = await self._fast_lookup(track.video_id)
    if hit is not None:
        return await self._finalize_hit(track.video_id, hit)

    lock = self._locks.setdefault(track.video_id, asyncio.Lock())
    async with lock:
        hit = await self._fast_lookup(track.video_id)
        if hit is not None:
            return await self._finalize_hit(track.video_id, hit)
        return await self._download_locked(track)
```

**Where:** `src/ryzic/audio_cache.py` lines 158–186 (the `get_or_download` body); add `_finalize_hit` as a 3-line helper after `_fast_lookup`.

**Why safe:** Three identical 3-line blocks ARE the "≥3 callers" threshold the maintainer's rule names. Extracting saves ~6 LOC net (3 × 4 lines duplicated → 3 × 1 line + 4-line helper) AND makes the double-checked-locking pattern's "what happens on hit" answer visible at the helper's name. Nothing changes about correctness — the order of operations is preserved (touch → pin → return). The `_fast_lookup` itself stays as-is.

This is one of the few cases where the brief asks "is this earned?" and the honest answer is "the function it lives in *also* needs a small extraction to stop repeating itself."

**Estimated LOC saved:** ~6 LOC src.

---

### S-5 — `_evict_to_fit`'s loop body is fine; cut one comment that narrates the obvious

**What to cut/collapse:** `_evict_to_fit` (lines 290–317) is a clean 28-line function. The double-query (SUM first to early-exit, then full ORDER BY) is the right shape — the SUM is one row and avoids fetching candidates when not needed. The for-loop body is straightforward:

```python
for video_id, rel_path, size in candidates:
    if total <= self._max_bytes:
        break
    if self._in_use.get(video_id, 0) > 0:
        continue
    file_path = self._cache_root / rel_path
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        _log.warning("eviction failed to unlink %s", file_path, exc_info=True)
        continue
    await conn.execute("DELETE FROM entries WHERE video_id = ?", (video_id,))
    total -= int(size)
```

The brief asks "clean or convoluted?" — clean. **Keep as-is.** Each branch earns its place: the early-break is the loop's success criterion; the in-use skip is M1 §4's hard requirement; the OSError catch protects against the disk-on-fire test case (`test_eviction_unlink_failure_does_not_drop_row`); the row-DELETE only runs after unlink succeeds, preserving the "row stays so we can retry" invariant. Pulling any of these out would either lose semantic distinction or split a 12-line function into a 12-line function plus three named-but-trivial helpers.

The one trim available: the `tmp_path.unlink(missing_ok=True)` comment in `_download_locked` (lines 202–203):

```python
# ``os.replace`` is atomic on the same filesystem — readers
# never see a partial file at ``final``.
os.replace(tmp_path, final)
```

This is a real WHY (atomicity is the load-bearing property), but the second sentence ("readers never see…") restates the first. Trim to one line:

```python
os.replace(tmp_path, final)  # atomic on the same FS — no partial reads
```

**Where:** `src/ryzic/audio_cache.py` lines 202–204.

**Estimated LOC saved:** ~2 LOC. **Low conviction; cosmetic.**

---

### S-6 — PRAGMA bookkeeping / connection setup — `executescript` already batches

**What to cut/collapse:** `open()` currently does:

```python
self._conn = await aiosqlite.connect(self._db_path)
await self._conn.execute("PRAGMA journal_mode=WAL")
await self._conn.execute("PRAGMA synchronous=NORMAL")
await self._conn.executescript(_SCHEMA)
await self._conn.commit()
```

The two PRAGMAs and the schema can be one `executescript`:

```python
self._conn = await aiosqlite.connect(self._db_path)
await self._conn.executescript(
    "PRAGMA journal_mode=WAL;\n"
    "PRAGMA synchronous=NORMAL;\n"
    + _SCHEMA
)
await self._conn.commit()
```

Or, if the caller prefers the PRAGMAs visible-not-buried, fold them into `_SCHEMA`'s string:

```python
_SCHEMA: Final = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS entries (
  ...
);
CREATE INDEX IF NOT EXISTS entries_lru ON entries(last_used_ts);
"""
```

The latter is cleaner — `_SCHEMA` becomes "everything to put the database in the desired state at startup," which is what the variable name already implies. The two `await self._conn.execute(...)` lines disappear.

**Where:** `src/ryzic/audio_cache.py` lines 121–125 (`open` body) and lines 49–63 (`_SCHEMA` definition).

**Why safe:** `aiosqlite.executescript` runs multiple statements in one round trip and commits on its own (modulo aiosqlite's autocommit handling — the explicit `commit()` afterward is harmless). PRAGMAs are statements; SQLite accepts them inside a multi-statement script the same as `CREATE TABLE`. The `test_open_sets_wal_pragmas` test (already in the suite) verifies the result, so any shape that produces the same pragmas-applied state passes unchanged.

The one thing to verify in implementation: aiosqlite's `executescript` may not honor PRAGMA inside a multi-statement script with the same semantics as `execute` (some sqlite drivers strip PRAGMAs from scripts). If the test fails after the move, fall back to keeping the two `execute` lines and at least dropping the explicit `await self._conn.commit()` — `PRAGMA journal_mode=WAL` is auto-committed; `synchronous=NORMAL` is connection-scoped and doesn't need commit; only the `executescript` of `_SCHEMA` matters.

**Estimated LOC saved:** ~3 LOC. **Medium conviction** — verify aiosqlite's PRAGMA-in-script behavior before committing the change.

---

### S-7 — Test parameterization: open-lifecycle quartet → one parameterized test

**What to cut/collapse:** `test_open_creates_directories_and_schema`, `test_open_sets_wal_pragmas`, `test_open_is_idempotent_across_restarts`, `test_methods_raise_when_not_opened` are four separate functions probing four facets of `open` / `close`. Three of them (the first three) follow the same scaffold:

```python
c = AudioCache(tmp_path, max_bytes=10_000)
await c.open()
try:
    # one assertion
finally:
    await c.close()
```

Three back-to-back, each with one assertion. Standard parameterization shape:

```python
@pytest.mark.parametrize(
    ("check_name", "do_check"),
    [
        ("dirs_and_schema", _check_dirs_and_schema),
        ("wal_pragma",      _check_wal_pragma),
        ("idempotent_reopen", _check_idempotent_reopen),
    ],
)
async def test_open_lifecycle(tmp_path, check_name, do_check):
    c = AudioCache(tmp_path, max_bytes=10_000)
    await c.open()
    try:
        await do_check(tmp_path, c)
    finally:
        await c.close()
```

…**but** this is one of those cases where parameterization makes the file *less* readable (named lambdas-as-fixtures, indirection between the parameter id and the actual assertion). The three tests are short enough that inline-function-per-case is also fine. **Recommend:** leave the open-lifecycle tests as-is; the duplication is structural, not behavioral, and the function names document what each pins.

The `test_methods_raise_when_not_opened` is genuinely a different shape (no `open()` call), keep separate regardless.

**Estimated LOC saved:** 0. **Recommend skipping** — parameterization here would be motion without progress. Test count noise is real but the four open-lifecycle tests already do four distinct jobs. (See S-8 for the parameterizations that do earn their keep.)

---

### S-8 — Eviction trio could parameterize the time-warp setup

**What to cut/collapse:** `test_eviction_removes_oldest_first` and `test_eviction_skips_in_use_entries` (lines ~250–310) repeat the same time-warping pattern:

```python
with _patch_download(_bytes_payload(1000)):
    path_a = await cache.get_or_download(a)
    await cache.release(a.video_id)  # or NOT — that's the variant
with (
    patch("ryzic.audio_cache.time.time", return_value=time.time() + 10),
    _patch_download(_bytes_payload(1000)),
):
    path_b = await cache.get_or_download(b)
    await cache.release(b.video_id)
with (
    patch("ryzic.audio_cache.time.time", return_value=time.time() + 20),
    _patch_download(_bytes_payload(1000)),
):
    path_c = await cache.get_or_download(c)
```

The two tests differ in **one line**: `release(a.video_id)` vs nothing. The assertion afterward differs (which file survived). A small helper extracts the time-warp + download:

```python
async def _add_track(cache, track, *, payload_bytes=1000, advance_seconds=0):
    with (
        patch("ryzic.audio_cache.time.time", return_value=time.time() + advance_seconds),
        _patch_download(_bytes_payload(payload_bytes)),
    ):
        return await cache.get_or_download(track)
```

Each test then writes:

```python
path_a = await _add_track(cache, a)
await cache.release(a.video_id)
path_b = await _add_track(cache, b, advance_seconds=10)
await cache.release(b.video_id)
path_c = await _add_track(cache, c, advance_seconds=20)
```

**Where:** `tests/test_audio_cache.py` `test_eviction_removes_oldest_first` and `test_eviction_skips_in_use_entries` (lines ~250–340).

**Why safe:** The `with patch(... time.time, return_value=time.time() + 10)` boilerplate is repeated **6+ times** in the eviction tests alone. The helper is a 5-line function that lives in the test module; both eviction tests shrink by ~5 LOC each, and a future test adding a third eviction scenario gets the same shape for free. Per the maintainer's "≥3 callers" rule — the time-warp + patched-download is a 6-caller pattern.

**Estimated LOC saved:** ~10 LOC tests.

---

### S-9 — Drop `_fake_download(payload)` indirection; inline `_patch_download`

**What to cut/collapse:** `_fake_download` (lines 84–95, 12 LOC) returns an async stub; `_patch_download` (lines 97–101, 5 LOC) wraps it in a `patch.object`. Two-layer indirection for what's a one-liner per call site:

```python
def _patch_download(payload: bytes = _M4A_HEAD * 8) -> Any:
    async def _impl(url: str, dest: Path, *, cache_root: Path) -> None:
        dest.write_bytes(payload)
    return patch.object(audio_cache, "download", side_effect=_impl)
```

Drop `_fake_download` as a separate name; the inner `_impl` lives entirely inside `_patch_download`'s body. (S-1 cuts `_M4A_HEAD` too — the default becomes whatever non-empty bytes makes the test pass, e.g. `b"audio-bytes-stand-in"`.)

**Where:** `tests/test_audio_cache.py` lines 84–101.

**Why safe:** `_fake_download` has exactly one caller (`_patch_download`) and exists only for type symmetry with the production `download` function. Inlining loses no clarity — the closure shape is canonical for `side_effect`. Saves the named-but-trivial intermediate.

**Estimated LOC saved:** ~6 LOC tests.

---

### S-10 — The 4 uncovered defensive `OSError` branches in `sweep_orphans` — drop the inner one

**What to cut/collapse:** The PR description flags:

> Coverage on the new module: **98%**. The 4 uncovered lines are defensive OSError branches in `sweep_orphans`.

There are two `OSError` branches in `sweep_orphans`:

1. **`mtime = file_path.stat().st_mtime`** (lines 355–358) — wrapped in try/except; on OSError, `continue` (skip this file).
2. **`file_path.unlink()`** (lines 361–365) — wrapped in try/except; on OSError, log warning + continue.

The brief asks: "defensive at internal boundary, drop?" Honest answer:

- **Branch #2 (unlink failure)** has a parallel in `_evict_to_fit` (lines 312–314: also a logged-warning continue), and that one IS exercised by `test_eviction_unlink_failure_does_not_drop_row`. The cache subsystem has a consistent posture: "I/O failures during cleanup get logged and don't bring the system down." Sweep should match. **Keep #2; the audit trail matters when a cron-driven sweep silently fails.**
- **Branch #1 (stat failure)** is the more defensive one. The file appeared in `rglob("*")` 3 lines earlier; for `stat()` to fail in those 3 lines requires concurrent deletion (another sweep, manual `rm`, or the cache's own evictor running at the same time). M1 §4 says sweep is invoked "on startup" — i.e. before `AudioCache.open()`, before any concurrent activity. The race window is real but operationally negligible; on the rare miss, the file gets re-considered next startup. **Drop the try/except** on `stat()`; let the OSError propagate. If the operator hits it, they get a clean traceback that points at the file. Saves 4 LOC.

**Where:** `src/ryzic/audio_cache.py` lines 355–358.

**Why safe:** Sweep is a startup-time, single-instance, best-effort cleanup. The stat-failure branch protects against a class of failures that doesn't happen in M1's deployment model (no parallel sweeps, no manual concurrent rm). The other OSError branch (unlink) protects against a different, real failure mode (read-only mount, permission rotation) and stays. Trimming the unreachable one matches the maintainer's "no half-finished features" — this defensive code never fires in practice and isn't part of any test.

**Estimated LOC saved:** ~4 LOC src + drops the "98% coverage" footnote (becomes ~99%, which is its own reviewer-comfort win).

---

### S-11 — Comments narrating WHAT — small set, mostly already restrained

**What to cut/collapse:** The source file is unusually disciplined about comments — most of what's there is real WHY (review-§4 race notes, double-checked locking, lock-leak rationale). The cuttable WHAT-narrators:

- **Line 173–174** in `get_or_download`: `# Pins the entry via _in_use[video_id] += 1 BEFORE returning ...` is a docstring summary that restates what the next 4 lines visibly do. The "caller MUST release exactly once" half is real WHY and stays. Trim the first sentence; keep the contract.
- **Line 179–180** in `get_or_download` lock branch: `# Double-checked locking — another coroutine may have finished the download while we waited on the lock.` — borderline; the term "double-checked locking" is the full WHY, but the next sentence narrates what "double-checked" means. **Borderline keep** for vocab-of-art clarity.
- **Line 244–246** in `release`: `# Defensive: a double-release after the entry already hit zero (and was popped). Counter[key] returns 0 by default rather than raising; we simply ignore.` — this IS real WHY (explains the swallowed-no-op), keep.
- **Line 326–327** in `sweep_orphans` docstring: `Returns the count deleted, primarily for tests.` — WHAT-restatement of the type signature. Cut "primarily for tests" (it's also the natural return for a CLI invocation).

Also the per-class comments on lines 137–141 (`# Locks intentionally leaked: ...` and `# Counter (not set) ...`) are both real WHY referencing the simplify doc and review notes — **keep**.

**Where:** small scattered cuts, ~4 LOC src across 3 sites.

**Estimated LOC saved:** ~4 LOC. **Low conviction; cosmetic.**

---

### S-12 — `_audio_path`'s `relative_to` defense — keep as-is, but the test could be a doctest

**What to cut/collapse:** Nothing structural. `_audio_path` (lines 76–91) is the right shape: `validate_video_id` early, `Path` construction, post-resolve `relative_to` check. The 4-line docstring is real WHY (defense-in-depth against future widening).

The three tests (`test_audio_path_rejects_traversal_via_symlink`, `test_audio_path_validates_video_id_first`, `test_audio_path_layout`) cover three distinct properties — keep separate. **No cut.**

**Estimated LOC saved:** 0.

---

### S-13 — `release` early-return uses `<= 0`, then immediately checks `<= 0` again

**What to cut/collapse:** `release` (lines 236–251) is:

```python
async def release(self, video_id: str) -> None:
    if self._in_use[video_id] <= 0:
        self._in_use.pop(video_id, None)
        return
    self._in_use[video_id] -= 1
    if self._in_use[video_id] <= 0:
        del self._in_use[video_id]
```

The two `<= 0` checks are doing different things (early-return vs post-decrement cleanup), but the body could collapse to:

```python
async def release(self, video_id: str) -> None:
    """Drop one in-use reference; remove the key when it hits zero."""
    count = self._in_use[video_id] - 1
    if count > 0:
        self._in_use[video_id] = count
    else:
        # Counter[key] returns 0 by default for missing keys, so this
        # also handles the double-release-after-zero case as a no-op.
        self._in_use.pop(video_id, None)
```

Same semantics; one branch instead of two; the "Counter[key] returns 0" comment migrates next to the `pop` where it's load-bearing.

**Where:** `src/ryzic/audio_cache.py` lines 236–251.

**Why safe:** `Counter` access defaults to 0 for missing keys, so `self._in_use[video_id] - 1` is `-1` on a missing key — the `count > 0` branch correctly takes the else path, and `pop(..., None)` is a no-op on a missing key. Two distinct paths collapse to one. Net: ~3 LOC source + the docstring's "Defensive:" paragraph also collapses (the no-op is now self-documenting).

**Estimated LOC saved:** ~5 LOC src.

**Counter-note:** This is a pure-shape collapse and could mildly hurt readability for someone scanning for "what does `release` do on a double-release?" The current spelling makes the double-release branch explicit. Net: optional, low-conviction. If the test count flags `test_release_idempotent_after_zero` it's the right test to keep regardless.

---

### S-14 — Things considered and kept as-is

These I considered cutting and decided not to.

- **The `Counter[_in_use]` choice over `set` or `dict[str, int]`** — keep. M1 §4 spec calls it out (review §4: "Counter (not set) because the same video may be queued multiple times"). The default-zero semantics are load-bearing for `release`'s simplicity.
- **`_locks` deliberately leaked** — keep. `M1-simplify.md` §10 keep-list locks this in.
- **The `_FALLBACK_EXT = "audio"` constant** — keep even if S-1 lands. The `ext` column in sqlite still wants a value, and "audio" is the right one.
- **`_ORPHAN_MIN_AGE_S` as 1 hour (Final constant)** — keep. The 4-line comment above it is real WHY (race-with-concurrent-download).
- **`_DB_FILENAME` / `_AUDIO_SUBDIR` / `_TMP_SUBDIR` as `Final` constants** — keep. Three sites each, used in `__init__` and `sweep_orphans` and `_audio_path`. Earned. (Mirrors S-12's verdict in `PR5-simplify.md` rejecting `_PLAYLISTS_DIR` for the *opposite* reason — that one had only one production callsite.)
- **`_require_conn` raising RuntimeError when `_conn is None`** — keep. The contract is "open before use" and surfacing the misuse with a friendly RuntimeError beats `AttributeError: 'NoneType' object has no attribute 'execute'` from inside a deeply nested async call.
- **The double-checked locking in `get_or_download`** — keep. Canonical pattern for collapse-to-one-download under contention; the second `_fast_lookup` after lock acquisition is the price.
- **Atomic `os.replace(tmp, final)` after download** — keep. Standard idiom; readers (the in-flight `_fast_lookup` from another coroutine) never see a half-written file at `final`.
- **Module docstring** — keep. Every paragraph carries non-obvious WHY (the lifecycle, the in-memory state vs sqlite-as-durable-index distinction, the deliberate-no-TTL choice).
- **`async def open` doing `mkdir(exist_ok=True)` for all three subdirs** — keep. One loop, one comment-free body, three real directories the cache requires.
- **The `INSERT OR REPLACE INTO entries` SQL** — keep. The "OR REPLACE" is the load-bearing semantic for `test_stale_row_with_missing_file_is_repaired`.
- **`_touch` as a separate async method** — keep. Three callers (top hit branch, lock-protected hit branch, hit-after-lock branch via S-4's `_finalize_hit`). Earns the name.
- **Test file's `_track(...)` factory** — keep. Used by ~25 tests with per-test overrides; the factory pattern is the canonical pytest shape.
- **`_read_one` / `_read_many` test helpers** — keep. Used by ~6 tests each; saves the `async with conn ... cur ... fetchone()` boilerplate.
- **The path-traversal symlink test** — keep. The post-resolve `relative_to` defense IS the security boundary; the test pins it.
- **The `test_concurrent_get_or_download_triggers_one_download` test** — keep. M1 §11 acceptance criterion #3 ("Two simultaneous /play for same URL → exactly one yt-dlp download"). Load-bearing for sign-off.
- **`test_just_inserted_file_not_evicted_due_to_pin`** — keep. Regression for review §4 race; pinpoints a specific race condition; non-obvious from other eviction tests.
- **`test_eviction_unlink_failure_does_not_drop_row`** — keep. Documents the "row stays, retry next time" contract.
- **`# ---` ASCII section banners in the test file** — borderline. `PR5-simplify.md` S-8 cut these; `PR3-simplify.md` made the same call. For consistency: cut them here too (~12 LOC). But this is decoration-policy bikeshed; not in the top 3.

---

## Top 3 simplification wins by impact

1. **S-1 — Drop `_detect_ext` magic-byte sniffing.** ~50 LOC (21 src + 30 tests + 4 magic-byte fixture constants). The PR's own docstring concedes the suffix is decorative; Lavalink reads the body. This is the single biggest "earned vs unearned" call in the PR — solves a problem (codec-aware on-disk layout for debugging) that the spec doesn't ask for. If a future contributor needs it, 8 lines + 1 test will bring it back.

2. **S-2 + S-9 — Cut the speculative helpers (`release_many`, `_fake_download`).** Together ~30 LOC. Both have zero or one caller and exist for namespace tidiness rather than code reuse. The `release_many` cut is the more important one (it's a public API surface that consumers don't need); `_fake_download` is internal test scaffolding.

3. **S-4 + S-8 — Targeted helper extractions where the duplication IS at threshold.** `_finalize_hit` (3-caller hit-finalization) saves ~6 LOC AND makes the double-checked locking cleaner; `_add_track` test helper saves ~10 LOC of repeated time-warp boilerplate. ~16 LOC together. The mirror of S-2: where extraction earns its keep, extract; where it doesn't, inline.

---

## Keep as-is

- The class shape (`AudioCache` with sqlite + in-memory state) — earned per `M1-simplify.md` §10's class/function distinction.
- `_evict_to_fit`'s loop body — clean; each branch earns its place; no extraction available without losing semantic distinction.
- `_fast_lookup` as a read-only function — correct shape; the docstring's WHY is load-bearing.
- `sweep_orphans` at module level (not a method) — correct per spec; lets future CLI/cron invoke without instantiating the cache.
- `_audio_path`'s post-resolve `relative_to` defense — load-bearing security boundary.
- The `release` body — could be collapsed (S-13) but the explicit double-release branch documents the contract; leave unless the maintainer prefers terseness.
- The double-checked locking pattern in `get_or_download`.
- Atomic `os.replace` after download.
- All module-level `Final` constants (`_DB_FILENAME`, `_AUDIO_SUBDIR`, `_TMP_SUBDIR`, `_ORPHAN_MIN_AGE_S`, `_FALLBACK_EXT`).
- The `INSERT OR REPLACE` for stale-row repair.
- Test factory `_track(...)` and helpers `_read_one` / `_read_many`.
- The 3 acceptance-pinning tests (concurrency, eviction-removes-oldest, eviction-skips-in-use) and the regression test for the just-inserted-file-pin race.
- The 4-line comment above `_ORPHAN_MIN_AGE_S` (the WHY behind the magic 1h number).

---

## Total impact

- **LOC saved (source):** ~40–50 (S-1: 21, S-2: 8, S-3: 1, S-4: −4 net after adding `_finalize_hit` helper but eliminating 3 duplicates of 4-line block ≈ 6 saved, S-5: 2, S-6: 3, S-10: 4, S-11: 4, S-13: 5).
- **LOC saved (tests):** ~60–80 (S-1: 30, S-2: 16, S-8: 10, S-9: 6, plus banner trim ~12).
- **Realistic merged total: ~100–130 LOC** out of +956, ~10–14%. Most of it is one judgment call (S-1: do we need ext sniffing?) plus standard "cut speculative helpers + parameterize repeated test setup" hygiene.
- **Net file ratio:** PR drops from 362/594 (1:1.6) toward ~320/520 (1:1.6) — the test-to-source ratio stays roughly the same, which is the right answer: a security-sensitive cache subsystem with sqlite + concurrency + path safety + eviction earns its high test coverage.

The brief's "test scaffolding (35 cases for 360 LOC source) — too rich?" diagnosis was *partly* right — the cache subsystem's surface (open/close, get/release, eviction with pinning, orphan sweep, path safety) genuinely needs heavy testing. The bloat is concentrated in the codec-detection branch (which S-1 dissolves entirely) and the eviction-test time-warp boilerplate (S-8). Other test groups (concurrent download, eviction acceptance, sweep age guard) are right-sized.

The brief's "release_many vs callers calling release per video_id in a loop — earned helper?" diagnosis was right — not earned. The brief's "_fast_lookup read-only optimization — earned?" was wrong direction; the read-only design is correct, but the surrounding *call pattern* duplicates the "hit finalization" three times (S-4 catches the real smell). The brief's "_detect_ext — necessary?" is the load-bearing question; the answer is no, by the PR's own admission of decorative purpose.

**Honest verdict:** PR6 is *mostly* tight, with one real "is this earned?" call (`_detect_ext`) and one real "speculative helper" call (`release_many`). The eviction loop, double-checked locking, atomic replace, and orphan sweep are all earned. No subsystem-level cuts; this is targeted polish + one judgment call on codec detection.

---

## Report

(a) File saved at `/home/user/Projects/ryzic/docs/plans/PR6-simplify.md`.

(b) Top 3:
   1. S-1 — Drop `_detect_ext` magic-byte sniffing entirely (~50 LOC inc. tests + magic-byte fixture constants). PR's own docstring concedes the on-disk suffix is decorative since Lavalink sniffs the body.
   2. S-2 + S-9 — Cut speculative helpers `release_many` and `_fake_download` (~30 LOC). Zero / one caller; the `release_many` 2-line for-loop belongs at the call site.
   3. S-4 + S-8 — Targeted extractions where ≥3 callers genuinely duplicate: `_finalize_hit` collapses the touch+pin+return pattern in `get_or_download`; `_add_track` test helper absorbs the repeated time-warp + patched-download setup (~16 LOC).

(c) Total LOC the PR could lose: **~100–130 LOC** out of +956, ~10–14%. One judgment call (codec detection) plus standard hygiene (cut speculative helpers, extract real duplication, parameterize repeated test setup). No structural rewrites; surface contract for `AudioCache` and `sweep_orphans` unchanged.
