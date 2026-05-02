# PR #6 Review — `feat(cache): audio cache (sqlite-LRU + per-video lock)`

**Branch:** `feat/audio-cache` -> `main`
**Scope per plan:** `docs/plans/M1.md` §4 (audio cache subsystem). Implements PR3b per the §12 PR breakdown.
**Diff:** 2 files changed, +956 / -0. `src/ryzic/audio_cache.py` (+362), `tests/test_audio_cache.py` (+594).
**Local verification:** `uv run pytest tests/test_audio_cache.py -q` -> 35 passed in 0.19s. PR description claims 98% coverage on the new module. Spot-checked path-safety, race-fix ordering, schema, WAL pragmas, and orphan-sweep behavior.

The implementation closely tracks plan §4 and the explicit decisions called out in the brief. The code is clean, comments are WHY-only and load-bearing, SRP is respected (cache stays guild-agnostic; caller passes `TrackInfo`). Two genuine race issues found, neither catastrophic but both visible to users under specific timing; remaining findings are LOW polish.

---

## Findings

### MEDIUM-1 — Fast-path TOCTOU: `_fast_lookup` -> `_touch` -> `_in_use += 1` has a race window where a concurrent eviction can delete the file before it is pinned

- **Severity:** MEDIUM
- **Where:** `src/ryzic/audio_cache.py:165-169` (the fast-path branch in `get_or_download`)
- **Why it matters:** The plan's whole point of the in-use Counter is "eviction never deletes a file Lavalink is currently streaming." The slow path (`_download_locked`, line 226) correctly pins BEFORE running the evictor — that's the §4 race fix the brief specifically calls out. The fast path has the *symmetric* race in the *opposite* direction:

  1. Coroutine A: `await self._fast_lookup(track.video_id)` returns path P (file exists at this instant) — line 165.
  2. Coroutine A: `await self._touch(track.video_id)` — UPDATE + commit. **This is an `await` and yields the event loop.**
  3. Coroutine B (concurrent `/play` for some *other* video): finishes `_download_locked`, pins itself, and runs `_evict_to_fit`. The evictor scans `ORDER BY last_used_ts ASC`, finds A's video_id with `_in_use.get(vid, 0) == 0` (A hasn't pinned yet), unlinks the file, DELETEs the row.
  4. Coroutine A resumes: `self._in_use[track.video_id] += 1`, returns P. **P now points at a non-existent file**, and the row no longer exists in sqlite.

  The blast radius is bounded — the very next play of the same id repairs via the lock-protected re-download branch — but the user-visible symptom is a Lavalink `LOAD_FAILED` for what looked like a hot cache hit. This directly contradicts the invariant the §4 plan and the module docstring promise. It is also the exact failure mode the slow-path race fix at line 226 was written to prevent; the symmetric fast-path version was missed.

  Triggering it in the wild is not contrived: the evictor only runs when the cache is over capacity, which is the normal steady state for a healthy cache.

- **Fix:** Swap the order so the pin happens *before* the awaited `_touch`. `_in_use[vid] += 1` is synchronous and cannot yield, so once it runs no concurrent evictor will pick this id:

  ```python
  hit = await self._fast_lookup(track.video_id)
  if hit is not None:
      # Pin BEFORE _touch (sync, no yield) — symmetric to the
      # slow-path fix in _download_locked. Otherwise a concurrent
      # download's evictor can delete the file between _fast_lookup
      # and the +=1.
      self._in_use[track.video_id] += 1
      await self._touch(track.video_id)
      return hit
  ```

  Apply identically to the double-checked branch at line 178. Add a regression test mirroring `test_just_inserted_file_not_evicted_due_to_pin` but for the fast path: prime an entry, second concurrent `/play` waits inside `_touch` while a third (different vid, large payload, low `max_bytes`) forces eviction; assert the first vid's file still exists when its `get_or_download` returns.

---

### MEDIUM-2 — Commit visibility: `INSERT OR REPLACE` and `_evict_to_fit` ordering can leave the on-disk file orphaned across a crash

- **Severity:** MEDIUM
- **Where:** `src/ryzic/audio_cache.py:198-227` (`_download_locked`)
- **Why it matters:** The current order is: `os.replace(tmp, final)` -> `INSERT OR REPLACE` -> `commit()` -> `_in_use[vid] += 1` -> `_evict_to_fit()` (which has its own `commit()`). If the process crashes between `os.replace` and `commit()`, the file is on disk under `audio/...` but no row exists — the orphan sweep at next startup will eventually reclaim it (after 1h). That's acceptable.

  The subtler case is in `_evict_to_fit` itself (lines 298-311): the loop unlinks the file *first*, then issues the DELETE, then loops to the next candidate, then a single `commit()` at the end. If the process crashes mid-loop, files have been unlinked but the DELETE is uncommitted in the WAL — on restart, sqlite will roll back the DELETEs and you have rows pointing at files that don't exist. `_fast_lookup` handles this gracefully (returns None when `path.exists()` fails) — so the user-visible behavior is "next play re-downloads", which is fine. **But** the now-stale row keeps occupying byte-budget in the SUM query at the next eviction, so the cache's effective capacity is silently understated until the row is overwritten by a fresh download for the same id (unlikely in practice — evicted videos by definition aren't recently played).

  Compounded: an unlink that fails for I/O reasons (line 305 OSError branch) correctly keeps the row + logs WARN — that's the brief's specified behavior. But the `total -= int(size)` is only updated on success, so the loop will keep trying to evict more candidates. Good.

- **Fix:** Two cheap improvements, either or both:
  1. In `_evict_to_fit`, commit *per iteration* (DELETE then commit then unlink, or unlink then DELETE then commit). The commit is cheap under WAL+NORMAL and bounds the inconsistency window to a single entry. The brief's "unlink failure keeps row + logs WARN" guarantee is preserved either way.
  2. In `get_or_download`'s fast path, when `_fast_lookup` returns None for an existing-but-stale row, *delete the row* under the lock (the lock prevents the originally-cited race — concurrent INSERT can't fire because the lock is held by the repairing coroutine). Currently the stale row is repaired only by `INSERT OR REPLACE` after a successful re-download, leaving a window where a hard re-download failure would leave the row dangling.

  Neither is critical — the orphan sweep eventually reaps everything — but #1 in particular tightens an invariant that the rest of the code seems to assume holds.

---

### LOW-1 — `tmp/` partial-download path drops the `{ext}` suffix the plan specifies

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:183` (`_download_locked`), versus M1.md §4 storage layout
- **Why it matters:** Plan §4 says `tmp/{video_id}.{ext}.partial`. The PR uses `tmp/{video_id}.partial`. The deviation is *justified* and documented in the PR description: ext isn't known until after download (yt-dlp doesn't surface it cleanly through PR3a's narrow API; the magic-byte sniff in `_detect_ext` runs against the partial file). The `os.replace` to `audio/{vid[:2]}/{vid}.{ext}` happens after sniffing, so the final layout matches the plan.

  Worth noting because (a) the brief explicitly asks "schema matches exactly" — this is a tmp-path deviation, not a schema deviation, but worth flagging — and (b) if anything in the runtime ever scans `tmp/` for files matching `*.{ext}.partial` it will miss them. Currently nothing does.

- **Fix:** Not strictly required. If you want to honor the plan literally, sniff the *partial* file's magic bytes during the download (or rename to add `.ext.partial` after the first chunk) — but that's a meaningful complication for no gain. Better fix: a one-line note in the M1.md §4 storage layout pointing at the deviation, OR a one-line comment at line 183 pointing back at the design decision in the PR description.

---

### LOW-2 — `release()` is `async def` but does no I/O; the false async signature locks a future where it must be

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:230-245` (`release`), `247-254` (`release_many`)
- **Why it matters:** `release()` is purely in-memory Counter manipulation — no `await`, no I/O. It is `async def` only so that callers do `await cache.release(vid)`. That's defensible (preserves a stable API across future versions where release might do I/O — e.g. update sqlite's `last_used_ts` to "released at" for analytics) and is pragmatic for the planned PR5 wiring.

  But: `release_many` iterates `await self.release(vid)` in a tight loop; if `release` ever DOES become I/O-bound, this becomes O(N) sequential awaits when a `gather` would be the right shape. And: the fast-path `_in_use[track.video_id] += 1` is *not* async — so the API is asymmetric (sync acquire, async release).

- **Fix:** Either keep both sync (`release` becomes `def release` — symmetric with the sync `+=` increment), or document a comment at line 230 explaining the deliberate asymmetry. Since neither operation does I/O today, sync is the simpler choice and matches SRP/KISS. Tests would only need to drop a few `await`s.

---

### LOW-3 — `validate_video_id(track.video_id)` runs twice on the slow path

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:163` (entry check) and `194` (in `_audio_path` -> `validate_video_id`)
- **Why it matters:** Pure DRY — the call at 163 already validated, and `_audio_path` at 194 re-validates. Two regex matches per slow-path download is meaningless cost (regex is precompiled with `re.compile`), but the fact that two layers each insist on validating suggests the contract is unclear: is `_audio_path` defensive against direct internal use, or does it trust the caller? The docstring at line 88-93 frames it as "defense-in-depth against future refactors that might widen the charset" — that's a reasonable reason to keep the second call. So this is style polish, not a fix request.
- **Fix:** Optional. If kept, the doc comment at 88-93 already justifies it well. If removed, drop the call inside `_audio_path` and rely on the entry-point validation in `get_or_download` plus the `relative_to` post-check.

---

### LOW-4 — `_fast_lookup`'s "stale row whose file vanished" path leaks disk-row inconsistency until next download

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:256-274` (`_fast_lookup`)
- **Why it matters:** When the row exists but the file is missing, `_fast_lookup` returns None. The caller then enters the lock-protected branch, downloads, and `INSERT OR REPLACE` repairs the row. Good. But: the row's stale `bytes` value continues to inflate the SUM in `_evict_to_fit` until that re-download completes. If many entries are in this stale state simultaneously (e.g. the deploy operator manually deleted a chunk of `audio/`), the cache will think it's over-capacity and aggressively evict valid pinned-or-not entries until the SUM matches reality.

  Mitigation: this is a degraded-state recovery path — the orphan sweep at startup is meant to handle the inverse case. Stale rows whose files have been externally deleted *after* startup are an operator-error class of fault.

- **Fix:** Either (a) accept as-is and document the rule in operator docs ("don't manually delete files from `.cache/audio/` while ryzic is running; restart to recover"), or (b) add a `cleanup_stale_rows()` module function symmetric to `sweep_orphans` that scans rows and drops ones whose files are gone (only safe under the lock for the same reason `_fast_lookup` doesn't delete). (a) is fine for M1.

---

### LOW-5 — `_evict_to_fit` reads the entire `entries` table sorted by `last_used_ts` in a single `fetchall()`

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:293-296`
- **Why it matters:** For the `RYZIC_CACHE_MAX_GB` default of 5 GB and average 5 MB tracks, that's ~1000 rows — fine. For a tuned-up self-host with 100 GB cache and 5 MB tracks, that's 20k rows pulled into a Python list every time the cache exceeds capacity by a single byte. The `entries_lru` index makes the SELECT cheap, but the marshalling cost is linear in the row count.
- **Fix:** Stream candidates with `LIMIT N` and re-query on the next iteration if not yet under cap, *or* iterate the cursor instead of `fetchall()`. Both are nontrivial because `_in_use` filtering is in-process — you'd need to fetch in batches. **Not worth doing for M1 capacity (5 GB default).** Worth a TODO comment if you want to remember it; otherwise a pure capacity-bound future-incident watch item.

---

### LOW-6 — `sweep_orphans` opens an aiosqlite connection independent of `AudioCache`'s; no cross-process or cross-call coordination

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:333-339`
- **Why it matters:** The PR description says `sweep_orphans` is "independently invokable from CLI / scheduled jobs", which is the right design — but if a CLI invocation runs *while* the bot is up, both will hold connections to the same sqlite. WAL handles concurrent readers, and the sweep only reads (no writes to sqlite itself, only unlinks). The cross-process correctness of WAL+NORMAL is well-defined here, so this is not a bug. But the `1h` orphan threshold assumes cross-process clock agreement and assumes no individual download legitimately takes >1h — which is enforced by yt-dlp's `max_filesize: 500_000_000` + reasonable network. Fine for M1.

  Watchlist item for a future "shared cache, multiple bot instances" deployment (which the plan does not currently support) — `_locks` is per-process and would not deduplicate downloads across instances. Two bot instances racing the same `/play` would both download, both `os.replace` (last wins atomically), both INSERT OR REPLACE (last wins). Not corrupt, just wasteful. Worth a note in the operator doc when multi-instance becomes a use case.

- **Fix:** None for M1. Capture the multi-instance limitation in the operator README when PR9 lands, or at the top of `audio_cache.py` ("single-instance assumed; cross-process locks not implemented").

---

### LOW-7 — `_detect_ext` unconditionally falls back to `"audio"` for unknown formats; future Lavalink versions may sniff more strictly

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:63-84`
- **Why it matters:** The fallback `"audio"` is harmless today because Lavalink's `LocalAudioSourceManager` content-sniffs the body. The on-disk suffix being `"audio"` is "decorative" per the comment at line 42-44, which is true. But the row in sqlite ALSO records `ext: "audio"` — if the row's `ext` is ever used for anything beyond the filename (e.g. metrics, format-specific decoder selection in a future PR), the `"audio"` value will be unhelpful. Currently nothing reads it programmatically.
- **Fix:** None. Worth a one-liner test confirming the sqlite row's `ext` column matches the on-disk suffix when fallback hits (currently only the fallback-with-empty-bytes case is parametrized in `test_detect_ext_recognizes_known_codecs`, not the round-trip through the cache). Optional.

---

### LOW-8 — Magic bytes for "OggS" header don't distinguish Opus from Vorbis containers

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:78-79`
- **Why it matters:** The format selector in PR3a's `_base_opts` is `bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio` — so "ogg with vorbis codec" is reachable only through the trailing `/bestaudio` fallback and is rare on YouTube. Even if it happens, `_detect_ext` would label it `"opus"` based on the OggS container. The on-disk suffix would be cosmetically wrong. Lavalink wouldn't care (sniffs the body).
- **Fix:** None. Worth a sentence in the docstring noting "OggS is treated as Opus; Vorbis-in-Ogg gets the same suffix and is exceedingly rare on YouTube" if precision matters to a future maintainer.

---

### LOW-9 — `release` is no-op when called on a vid never inserted; `release_many` accepts arbitrary iterables, including duplicates

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:230-254`
- **Why it matters:** `release_many([vid, vid])` decrements twice. If the caller (PR5/PR6a) accidentally builds the queue's vid list with duplicates (a very plausible bug in a later PR — same video queued twice triggers Counter hits 2; releasing the queue's vids must release each occurrence), behavior depends entirely on the caller building the list correctly. The cache is unable to detect "you over-released" beyond the soft `<= 0 -> pop` guard.

  This is correct per the §4 plan ("Counter not set, same video may be queued multiple times"), and the test `test_release_idempotent_after_zero` covers the over-release safety net. Just noting that `release_many` is not idempotent over the same id and the contract is "caller passes one vid per queue entry, including duplicates" — which the PR description gets right.

- **Fix:** Add the contract to the `release_many` docstring: "Pass one entry per queue position (duplicates expected for repeated tracks)." Currently the docstring says "iterates the guild's queue once and passes every video_id" which is close, but not explicit about duplicates.

---

### LOW-10 — `_audio_path` calls `cache_root.resolve()` on every invocation (not cached on the instance)

- **Severity:** LOW
- **Where:** `src/ryzic/audio_cache.py:98`
- **Why it matters:** `_audio_path` is module-level (no `self`), so it can't cache anything on the instance — but it also can't easily memoize across calls without polluting module state. `resolve()` does a syscall stack walk; on the cache hot path this runs once per slow-path download (rare). On `_audio_path` calls from tests (frequent), it doesn't matter. Genuinely negligible.
- **Fix:** None. If you find yourself profiling `_audio_path`, cache `cache_root.resolve()` on the `AudioCache` instance and pass it in. Not worth doing speculatively.

---

## Cross-cutting observations

- **Comments are WHY-only.** Spot-checked all 14 inline comments; every one explains a non-obvious *why*, none narrate the *what*. The multi-line module docstring + class docstring + method docstrings are well-calibrated for FOSS-grade onboarding without being preachy.
- **SOLID/SRP:** the design correctly keeps the cache guild-agnostic (caller passes the vid list to `release_many`); `sweep_orphans` is module-level rather than a class method, matching the spec; `_detect_ext` and `_audio_path` are module-level helpers (correctly so — they don't need instance state).
- **DRY:** one minor duplication noted above (LOW-3, double `validate_video_id`); otherwise tight.
- **KISS:** the implementation is genuinely small (362 lines including a generous module docstring). No premature abstractions, no extension hooks, no event emitters. Good.
- **Test quality:** 35 cases, ~600 lines, one race-fix regression test that maps directly to plan §4's specified race fix. Tests use `_M4A_HEAD * 8` payloads so `_detect_ext` runs against real bytes — that's the right rigor. The concurrent-download test (`test_concurrent_get_or_download_triggers_one_download`) correctly uses an `asyncio.Event` as a synchronization barrier instead of timing-dependent `asyncio.gather`. Good.
- **Async/sqlite:** `aiosqlite` shares a single connection across all coroutines via `self._conn`; aiosqlite serializes operations on a connection internally, so the implementation is safe but the connection is a serialization point. WAL+NORMAL is correctly applied per plan. The `_fast_lookup`'s `async with conn.execute(...)` pattern correctly releases the cursor — verified.
- **Cross-process / multi-bot watchlist:** captured in LOW-6.
- **No comment-as-narration anywhere I could find.** No `# Increment counter` or `# Acquire lock` style comments. Strong adherence to the maintainer's standard.

---

## Verdict

**minor revisions.**

MEDIUM-1 should be fixed before merge — it's a one-line reorder + a regression test, and it directly contradicts the cache's stated invariant. MEDIUM-2 is worth at least adding the per-iteration commit to `_evict_to_fit` (small change, tightens an invariant without complicating the code).

Everything else is LOW polish — the LOW-1 tmp-path deviation is justified and documented in the PR description and is fine to leave as-is. LOW-2 (sync `release`) is a judgment call worth a one-line decision either way. The remaining LOW items (4-10) are watchlist / future-incident notes, not blockers.

The implementer's six up-front decisions in the brief all check out: TrackInfo passed (no double round trip), `_fast_lookup` is read-only, magic-byte ext detection, module-level `sweep_orphans`, pin-before-evict in the slow path (correctly implemented at line 226), and unlink-failure WARN-and-keep-row (correctly implemented at line 307). The PR is materially correct and ships clean once MEDIUM-1 is addressed.
