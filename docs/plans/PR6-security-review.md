# PR #6 — Security Review

**PR**: `feat(cache): audio cache (sqlite-LRU + per-video lock)`
**Branch**: `feat/audio-cache` → `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commit reviewed**: `3542c4c`
**Files in scope** (PR diff only):

- `src/ryzic/audio_cache.py` (new, 363 lines)
- `tests/test_audio_cache.py` (new, 595 lines)

No dependency or build-system changes (`pyproject.toml`/`uv.lock` unchanged in this PR; `aiosqlite>=0.20` was already declared in PR #1, resolved at `0.22.1`). Out of scope per the brief: yt-dlp wrapper, playlist cache, Discord layer.

This review covers the ten focus areas from the brief: video_id input validation, path safety / traversal, TOCTOU on the orphan sweep, sqlite security under multi-instance, disk-fill enforcement, symlink-TOCTOU on the cache root, magic-byte ext detection, `release_many` arithmetic, test isolation / disk leaks, and dependency surface.

---

## Findings

### 1. `video_id` validation — charset/length, pre-construction

**Severity**: (informational — no finding)
**What**: `validate_video_id` (PR3a, `src/ryzic/ytdlp.py:31,77-80`) compiles `^[A-Za-z0-9_-]{6,20}$` and raises `InvalidVideoID` on mismatch. The audio-cache module enforces it at three of three sites BEFORE any path or sqlite work:

| Caller | Line | Order |
| --- | --- | --- |
| `_audio_path(...)` | `src/ryzic/audio_cache.py:94` | first statement of the function |
| `get_or_download(track)` | `src/ryzic/audio_cache.py:163` | first statement |
| `_audio_path` from `_download_locked` | invoked at line 194 | already validated by the entry point at 163 |

I exercised the regex against:

| Input | Outcome |
| --- | --- |
| `""` | rejected (length) |
| `"short"` (5 chars) | rejected (length) |
| `"a" * 21` | rejected (length) |
| `".dotted"` | rejected (charset — leading dot) |
| `".."` | rejected (length + charset) |
| `"../etc"` | rejected (charset — `/`, `.`) |
| `"%2e%2e"` | rejected (charset — `%`) |
| `"a%2fb%2f"` | rejected (charset — `%`, `/`) |
| `"NUL\x00d"` | rejected (charset — NUL) |
| `"CR\rid"` / `"NL\nid"` | rejected (charset) |
| `"unicodeㄒ"` | rejected (non-ASCII) |
| `"ＡＢＣＤＥＦ"` (full-width) | rejected (regex matches **raw** code-points; no NFKC pre-fold) |
| `"𝕒𝕓𝕔𝕕𝕖𝕗"` (mathematical double-struck) | rejected (raw code-points outside `[A-Za-z0-9_-]`) |
| `"with.dot"` | rejected (charset — `.`) |
| `"normal_id"` / `"dQw4w9WgXcQ"` / `"uppr0123_"` | accepted |

The regex is applied to the raw string; Python's `re.match` does NOT NFKC-normalize, so unicode confusables that look like ASCII (e.g. fullwidth `Ａ`) cannot bypass the allowlist. The `tests/test_audio_cache.py:291-295` case (`video_id="../../etc/passwd"`) confirms invalid IDs are rejected before `download` is invoked.

`release()` and `release_many()` deliberately do NOT validate (they manipulate an in-memory Counter, not paths or sqlite keys for INSERT). Reading a non-existent key from `Counter` returns 0 without creating an entry; `pop(key, None)` is also no-op for missing keys. Confirmed empirically — passing arbitrary strings to `release` does not grow `_in_use`. **Verdict: clean.**

---

### 2. Path safety — `relative_to(cache_root)` defends against in-cache symlink escape

**Severity**: (informational — no finding)
**What**: `_audio_path` (`src/ryzic/audio_cache.py:87-101`) constructs `cache_root / "audio" / video_id[:2] / f"{video_id}.{ext}"` then runs `final.resolve().relative_to(cache_root.resolve())`. `Path.resolve()` canonicalizes symlinks at check time. I exercised three escape vectors empirically:

- **Symlink shard inside `audio/`** — `audio/dQ` symlink → `/elsewhere`. The `resolve().relative_to()` call raises `ValueError`, mapped to `InvalidVideoID("audio path … escapes cache_root …")`. Covered by `test_audio_path_rejects_traversal_via_symlink` (line 482).
- **`cache_root` itself is a symlink** (e.g. `/var/cache/ryzic` → `/srv/audio-cache`). Both sides of `relative_to` resolve consistently to the canonical target; not flagged. Verified manually.
- **`video_id` containing `..`** — rejected by `validate_video_id` BEFORE path construction; covered by `test_audio_path_validates_video_id_first` (line 496).

`download()` (`src/ryzic/ytdlp.py:261-280`) duplicates the `relative_to` check on `tmp_path` before invoking yt-dlp; that's PR2's defense, but it's worth noting because PR3b's `_download_locked` builds `tmp_path` (line 183) without an explicit check — relying on the fact that `tmp_path = self._tmp_root / f"{validated_id}.partial"` is structurally in-bounds. The downstream `download()` re-checks. Belt-and-braces. **Verdict: clean.**

There IS a residual TOCTOU window: `_audio_path` resolves the parent at line 98, then `final.parent.mkdir` runs at line 195, then `os.replace` runs at line 198. A co-resident attacker with write access inside `audio/{shard}/` could race the symlink between the check and the move. This is the **same threat model** as PR2's #8 (LOW, deferred to PR3b). The deploy-time mitigation is "0o700 on `cache_root`, owned by the bot UID"; an O_NOFOLLOW-based fix is out of scope. Filed as #6 below.

---

### 3. Orphan sweep TOCTOU — `mtime > 1h` guard is not bullet-proof for slow downloads

**Severity**: LOW
**What**: `sweep_orphans` (`src/ryzic/audio_cache.py:314-362`) treats a file as orphan only if its `mtime` is **older** than 1h, intending to skip files that a concurrent download has just landed. The race the guard targets is:

1. Worker A calls `os.replace(tmp_path, final)` (line 198) — file appears in `audio/`.
2. Sweep enumerates `audio/`, sees the file, no row in sqlite, mtime young → **skipped** (correct).
3. Worker A inserts the row (line 205-220) — invariant restored.

That works for fresh downloads. **But `os.replace` PRESERVES the source's mtime** (verified: `os.utime(src, (old, old)); os.replace(src, dst)` yields `dst.mtime == old`). If a yt-dlp download takes >1h (slow connection, large `max_filesize=500_000_000`, retries), `tmp_path`'s mtime is already >1h ago. After `os.replace` the file lands in `audio/` with that old mtime. If `sweep_orphans` runs in the **microsecond gap** between `os.replace` (line 198) and the `INSERT` commit (line 221), the sweep deletes the just-moved file. Worker A then proceeds with INSERT — leaving a row pointing at a now-missing file.

The `_fast_lookup` path **handles** the resulting stale row (line 269-273: missing file is treated as miss, the lock-protected branch repairs by re-INSERT). So the worst-case is "next play of the same video re-downloads instead of hitting cache" — an availability blip, not corruption.

**Where**: `src/ryzic/audio_cache.py:198,221` (the gap); `src/ryzic/audio_cache.py:341-353` (the guard).

**Why it matters**: Extremely narrow. Requires (a) yt-dlp download taking >1h, (b) `sweep_orphans` invoked exactly in the gap. Operationally, sweeps run at startup only (per PR description). Not exploitable from a Discord-user URL.

**Fix**: Either (a) `os.utime(tmp_path, None)` before `os.replace` to reset mtime, OR (b) have `_download_locked` write the row BEFORE `os.replace` (would require `INSERT` on the not-yet-final `rel_path` and an UPDATE post-replace — more complex). Cheapest: a single `os.utime(tmp_path, None)` between line 197 and 198. One-line hardening.

---

### 4. SQLite security — connection string, PRAGMAs, multi-instance contention

**Severity**: LOW (multi-instance contention; not a single-instance finding)
**What**: `aiosqlite.connect(self._db_path)` (line 136) takes a `pathlib.Path` — no URI parsing, no `?immutable=1` exploit vector, no `:memory:` ambiguity. PRAGMAs:

- `journal_mode=WAL` — concurrent readers don't block the writer; tested explicitly in `test_open_sets_wal_pragmas`.
- `synchronous=NORMAL` — durable enough for cache contents (re-downloadable on loss); standard for WAL.

All SQL uses `?` placeholders (audited every `execute(`/`executescript(` call site at lines 139, 140, 141, 205-220, 265-267, 278-281, 287, 293-295, 309, 335). No string interpolation of user-controlled data into SQL. Schema CREATE statements are static. **Single-instance: clean.**

**Multi-instance scenario** (the brief explicitly asked): two ryzic processes pointing at the same `cache_dir`. SQLite WAL allows concurrent readers, but **all writers serialize on a single exclusive lock**. With aiosqlite's default `busy_timeout=5000ms` (verified empirically — `aiosqlite.connect` uses sqlite3's stdlib default of 5000ms), a contended write waits up to 5s before raising `OperationalError("database is locked")`. Realistic INSERT/UPDATE finishes in milliseconds, so practical contention is rare; under heavy concurrent `/play` storms across both instances it could surface.

There's a more concerning interaction with the `os.replace + INSERT` ordering. Process A:
1. `os.replace(tmp, final)` — file in `audio/`
2. `INSERT OR REPLACE` — **blocks for 5s waiting for B's lock, then `OperationalError`**
3. `_download_locked` raises; the file is orphaned in `audio/`
4. Eventually `sweep_orphans` cleans it (after 1h)

So a multi-instance deploy has a real "orphan file accumulation under contention" failure mode, not corruption. Per-video locks (`_locks`) coordinate WITHIN a process; they do not coordinate ACROSS processes. Two instances would both download the same video to the same destination in the worst case — `os.replace` is atomic so the file content is consistent, but both sqlite rows compete.

**Where**: `src/ryzic/audio_cache.py:136-142` (PRAGMAs), 198-221 (move-then-insert ordering).

**Why it matters**: M1 plan does not call out multi-instance support as a goal. M1 §4 explicitly says the cache is "shared across all guilds" of one bot, not across bot instances. The deploy doc should warn against pointing two bots at the same `cache_dir`.

**Fix**: Document the constraint in `M1.md` §4 and `.env.example` (e.g. "MUST be unique per ryzic instance — sqlite WAL serializes writers but does not coordinate `os.replace`-then-`INSERT` ordering across processes"). Optionally bump `busy_timeout` to e.g. 30000ms for resilience. Not blocking.

---

### 5. Disk fill — `_evict_to_fit` runs after every INSERT

**Severity**: (informational — no finding)
**What**: `_download_locked` (`src/ryzic/audio_cache.py:182-228`) always calls `await self._evict_to_fit()` (line 227) after `INSERT OR REPLACE`. The eviction loop:

1. `SELECT COALESCE(SUM(bytes), 0) FROM entries` — total of recorded sizes.
2. If `total <= max_bytes`, return (no work).
3. `SELECT video_id, rel_path, bytes FROM entries ORDER BY last_used_ts ASC` — oldest first.
4. For each candidate: skip if `_in_use[vid] > 0`; else `unlink(missing_ok=True)`, `DELETE`, decrement total.
5. Stop when `total <= max_bytes`.

Tested by `test_eviction_removes_oldest_first`, `test_eviction_skips_in_use_entries`, `test_just_inserted_file_not_evicted_due_to_pin`, `test_eviction_unlink_failure_does_not_drop_row`. The just-inserted entry is **pinned BEFORE** running eviction (line 226 → line 227), so even when its own size blows the cap (test at line 418-435 with `max_bytes=100` and a 1000-byte payload), it survives. This is the review §4 race fix and it's correctly implemented.

`_in_use[video_id] += 1` on the fast-path hit (lines 168, 178) also correctly pins entries returned to the caller, so a concurrent eviction triggered by another download cannot delete a file that is in transit to Lavalink.

`max_bytes` is enforced as bytes; `Config.cache_max_gb` (gigabytes, `_parse_positive_int(... default=5)`) is **not yet plumbed** into the cache (per the PR description's "Follow-ups" section — bootstrap wiring is deferred). When wired, the bootstrap code MUST multiply: `max_bytes = cfg.cache_max_gb * 1_000_000_000`. A future PR forgetting the multiplication would set the cap at 5 BYTES instead of 5 GB. Not a finding for THIS PR, but worth flagging as a watch-item for the bootstrap PR's review.

`unlink` failures are logged at WARNING and the row is preserved (line 305-307) so the next eviction retries — important so a single permission glitch doesn't silently corrupt the index.

One subtle gap: `_evict_to_fit` ignores files in `tmp/` AND any orphan files in `audio/` that the index doesn't know about (orphans are deleted by the separate `sweep_orphans`, which runs only at startup). So during runtime, orphan tmp files (e.g. from a crashed download) accumulate without bound until the next restart. See #7. **Verdict: cap-enforcement is clean; orphan-tmp accumulation is finding #7.**

---

### 6. Cache root is not validated to be a real directory at startup

**Severity**: LOW (defense-in-depth; deferred from PR2 §8)
**What**: PR2's security review (#8) flagged cache-directory symlink TOCTOU as "outside this PR's threat model, defer to M2 cache subsystem". This is M2's cache subsystem and the gap is unchanged. `AudioCache.open()` (lines 132-143) does:

```python
for d in (self._cache_root, self._audio_root, self._tmp_root):
    d.mkdir(parents=True, exist_ok=True)
```

There is no `os.path.realpath` check that `cache_root` is a real, owned, 0o700 directory rather than a symlink to e.g. `/etc`. If a co-resident attacker can plant a symlink at `cache_root` BEFORE the bot starts, the bot's writes land at the symlink target.

**Where**: `src/ryzic/audio_cache.py:132-143`.

**Why it matters**: Same threat model as PR2 #8 — requires co-resident write access to the bot's data directory. The existing post-construction `relative_to` check (#2) handles symlinks INSIDE the cache; this would close the symlink-AT-the-cache-root gap.

**Fix**: At the top of `open()`, add:

```python
if self._cache_root.is_symlink():
    raise RuntimeError(f"cache_root {self._cache_root!s} must not be a symlink")
# Or: refuse to start if cache_root.resolve() != cache_root.absolute()
```

Pair with a deploy-doc note that the cache directory should be owned by the bot UID with 0o700. Optionally check ownership/mode and warn. Not blocking.

---

### 7. `tmp/` orphan files are never swept

**Severity**: LOW (availability — disk fill on long-running bots with crash patterns)
**What**: `sweep_orphans` walks **only** `audio_root` (line 343 `audio_root.rglob("*")`). Files in `tmp/` are never enumerated. The download flow writes `tmp/{video_id}.partial`, then on success `os.replace`s it to `audio/`. On failure, line 187 calls `tmp_path.unlink(missing_ok=True)` — but only for `Exception` from `download(...)`.

Failure modes that leak tmp files:

- **Process crash mid-download** (SIGKILL, OOM, host reboot) — `tmp_path` survives indefinitely.
- **Partial download where yt-dlp itself wrote a `.part` file and exited cleanly without an exception** — unlikely but the `unlink(missing_ok=True)` only targets `tmp_path` (the `.partial` name), not yt-dlp's own intermediate `.ytdl`/`.part` files in the same `tmp/` dir.

Over months of uptime with a flaky network, `tmp/` could grow without bound. Not security per se — disk-fill DoS via persistent crash patterns.

**Where**: `src/ryzic/audio_cache.py:343` (sweep walks `audio_root` only); `src/ryzic/audio_cache.py:182-188` (failure cleanup is local to one `tmp_path`).

**Fix**: Extend `sweep_orphans` to ALSO walk `tmp_root`:

```python
tmp_root = cache_root / _TMP_SUBDIR
if tmp_root.exists():
    for file_path in tmp_root.iterdir():
        if not file_path.is_file():
            continue
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue
        try:
            file_path.unlink()
            deleted += 1
        except OSError:
            _log.warning("orphan sweep failed to unlink tmp file %s", file_path, exc_info=True)
```

Same age guard reused. ~10 lines + one test. Not blocking.

---

### 8. Magic-byte ext detection — closed return set, no path injection

**Severity**: (informational — no finding)
**What**: `_detect_ext` (`src/ryzic/audio_cache.py:63-84`) returns one of `{"m4a", "opus", "webm", "mp3", "audio"}` — five hardcoded string literals. None contain `/`, `\`, `.`, NUL, or `..`. The function reads only the first 16 bytes and returns the constant matched by the first prefix branch. A crafted yt-dlp output (audio bytes whose first 8 bytes are `\x00\x00\x00\x18ftyp...`) would yield `"m4a"` regardless of the actual container — but `"m4a"` is a safe path component. Lavalink's `LocalAudioSourceManager` sniffs the body itself, so the on-disk suffix is decorative.

The path is then constructed as `f"{video_id}.{ext}"` (line 95) — both pieces are constrained to safe character sets. The post-construction `relative_to` check (line 98) is the final guard. **Verdict: clean.** No way for a crafted download to inject a path component that breaks `relative_to`.

---

### 9. `release_many` cannot drive Counter negative; cannot un-pin uninvolved entries

**Severity**: (informational — no finding)
**What**: `release` (lines 230-245):

```python
if self._in_use[video_id] <= 0:
    self._in_use.pop(video_id, None)
    return
self._in_use[video_id] -= 1
if self._in_use[video_id] <= 0:
    del self._in_use[video_id]
```

The first guard prevents the counter from going negative. `release_many` is just a loop calling `release` per id. Verified by `test_release_idempotent_after_zero` (line 316-319). Counter[unknown_key] returns 0 without creating an entry, and `pop(..., None)` is a no-op for missing keys, so the behavior of releasing a never-acquired id is harmless.

The intra-coroutine atomicity holds because `release` is `async def` but contains no `await` — under asyncio's single-threaded scheduler, the body executes without interleaving. `release_many` iterates `for vid in video_ids` without yielding, which is fine for the bounded queue lengths in scope.

**There is no auth boundary here.** A "malicious caller" implies an internal API consumer, which is also the implementer. The internal API doesn't need to defend against itself; the design defends against caller bugs (double-release / missing-release) rather than malice. **Verdict: clean.**

One nit: a caller passing thousands of unique strings to `release_many` would briefly look up each in the Counter. Counter lookups don't materialize keys, so no growth. No DoS.

---

### 10. Tests do not leak files outside `tmp_path`; no real network

**Severity**: (informational — no finding)
**What**: `tests/test_audio_cache.py`:

- `_fake_download` (line 47-57) and `_patch_download` (line 60-62) replace `ryzic.audio_cache.download` with a stub that writes deterministic bytes to `dest`. The original `ryzic.ytdlp.download` is never invoked from any cache test. Confirmed by reading every test — `download` is patched in every test that exercises a download path.
- All cache instances are constructed with `tmp_path` (the pytest `tmp_path` fixture, function-scoped, auto-cleaned by pytest). No test writes outside `tmp_path`.
- `test_audio_path_rejects_traversal_via_symlink` creates `cache_root / elsewhere` siblings under `tmp_path`. Both are inside the test's tmp dir.
- No `requests`/`httpx`/`urllib.request`/`socket`/`aiohttp` import in `tests/test_audio_cache.py`. Confirmed by grep.

The `test_eviction_unlink_failure_does_not_drop_row` test (line 438-474) monkey-patches `Path.unlink` globally for its `with patch.object(Path, "unlink", ...)` block. The patch's `flaky_unlink` only raises for paths matching `a.video_id` (`"aaaaaaaaaaa"` — 11 lowercase a's, definitively not a real filesystem path). Other tests in the same file are not affected because pytest-asyncio function-scoped fixtures recreate `tmp_path` per test. **Verdict: clean.**

---

### 11. Dependencies — aiosqlite was already pinned; no CVEs at the resolved version

**Severity**: (informational — no finding)
**What**: `aiosqlite>=0.20` was already declared in PR #1's `pyproject.toml` (audited in PR1 security review §8). PR #6 does not modify `pyproject.toml` or `uv.lock` (`git diff main..HEAD -- pyproject.toml uv.lock` produces empty output). The resolved version `aiosqlite-0.22.1` (uploaded 2025-12-23, well after the 2025 patch line) has no outstanding CVEs known as of cutoff Jan 2026. aiosqlite is a thin asyncio wrapper around the stdlib `sqlite3` module — its attack surface is the same as `sqlite3`'s, which inherits sqlite's well-tested input handling.

The `aiosqlite.connect(path)` call uses a `pathlib.Path` argument; aiosqlite forwards to `sqlite3.connect`, which accepts a path string and opens the file with `O_RDWR|O_CREAT`. No URI-mode parsing (which would enable `?mode=memory&cache=shared` and similar), so an attacker who somehow controls `db_path` cannot trigger sqlite's URI-mode features. `db_path` is constructed as `cache_root / "index.sqlite"` from a config-supplied `cache_root`, not from user input — no injection vector. **Verdict: clean.**

---

## Summary

| Severity | Count |
| --- | ---: |
| HIGH (merge-blocking) | 0 |
| MEDIUM (fix soon) | 0 |
| LOW (nit / hardening) | 4 |
| Informational | 7 |

The four LOWs:

- **#3** — orphan sweep TOCTOU when an `os.replace`'d file inherits an old `mtime` (one-line `os.utime` fix; very narrow window).
- **#4** — multi-instance sqlite contention not coordinated across processes (deploy-doc note + optional `busy_timeout` bump; M1 doesn't promise multi-instance).
- **#6** — `cache_root` is not validated to be a real directory at startup (3-line `is_symlink` check; deferred from PR2 §8).
- **#7** — `tmp/` orphan files are never swept (extend `sweep_orphans` ~10 lines + 1 test; availability concern over month-scale uptime).

The brief's primary attack vectors — `video_id` charset bypass via traversal/unicode/encoding, in-cache symlink escape, sqlite injection, disk-fill via cap-enforcement gap, magic-byte path injection, `release_many` negative-counter, test-suite disk leakage — are **all defended**. The post-construction `Path.resolve().relative_to(cache_root.resolve())` is the load-bearing guard for path safety, and it is exercised by an explicit symlink-escape test. The "pin BEFORE evict" ordering (review §4 race) is correctly implemented and tested. SQL is fully parameterized; no string interpolation on user-controlled data anywhere.

Coverage is 98% on the new module per the PR description; spot-checked a sample of branches and the 4 uncovered lines really are defensive `OSError` swallowing.

## Verdict

**clean** — no merge-blocking issues. The four LOWs are hardening opportunities worth opening as follow-up issues:

- #3 & #6: cheap fixes, fold into the bootstrap PR (where `sweep_orphans` and `AudioCache.open()` get wired).
- #4: deploy-doc warning before the first multi-instance deployment lands.
- #7: 10-line extension to `sweep_orphans` next time the file is touched.

The cache module is the most attack-adjacent component in M1 (it persists artifacts derived from arbitrary YouTube URLs), and the implementer has correctly threat-modeled the path-safety, eviction-race, and concurrent-download surfaces. The defense-in-depth posture (validate-then-construct-then-relative-to-check) means a single bug in any one layer is caught by the next.
