# PR #5 Review — `feat(cache): playlist metadata cache (live-first with TTL fallback)`

**Branch:** `feat/playlist-cache` → `main`
**Scope per plan:** `docs/plans/M1.md` §5 (playlist metadata cache) + `M1-simplify.md` §3 (drop TTL env var) and §10 (module functions, no class). Implements PR4 per the §12 PR breakdown.
**Diff:** 2 files changed, +564 / -0. `src/ryzic/playlist_cache.py` (+209), `tests/test_playlist_cache.py` (+355).
**Local verification:** `uv run pytest tests/test_playlist_cache.py -v` → 42 passed in 0.13s. `uv run ruff check`, `ruff format --check`, `ty check` on the two new files all clean.

---

## Findings

### MEDIUM-1 — `is_stale` re-reads the cache file synchronously inside an async call site

- **Severity:** MEDIUM
- **Where:** `src/ryzic/playlist_cache.py:145-166` (`is_stale`)
- **Why it matters:** Per the plan §3 wording around the playlist embed, `is_stale` will be invoked from the `/play` command handler immediately after `fetch_with_fallback` returns. That handler is an `async def` running on the event loop. `is_stale` is `def` (not `async def`) and calls `_read_sync(path)` directly — so the event loop blocks for a `path.read_text(...)` + `json.loads(...)` round-trip on every `/play` of a playlist URL whose live fetch failed. Writes go through `asyncio.to_thread`; this is the only sync disk I/O in the module called from async land. `path.read_text` in particular touches the disk on the loop thread, which is what `asyncio.to_thread` exists to avoid.

  There is also a **double-read** in the cache-fallback path: `read()` already pulled and parsed the same JSON file moments earlier, and `is_stale(returned_info, cache_root=...)` does it again. So the branch most likely to surface staleness pays for two reads.

- **Fix (cheap, no API change):** make `is_stale` `async def` and route the disk hit through `asyncio.to_thread(_read_sync, path)`. While there, consider eliminating the double-read either by (a) caching `fetched_at` on the returned `PlaylistInfo` via a private sidecar field, or (b) returning `(info, fetched_at | None)` from `read()` so `fetch_with_fallback` can return the timestamp directly. Option (b) is the cleaner SRP play — the timestamp is cache-layer metadata, the dataclass stays clean, and `is_stale` collapses to a one-line `> _TTL_SECONDS` predicate against the timestamp the caller already has.

  The PR description's argument for re-reading ("keep `PlaylistInfo` clean") is sound, but blocking the event loop is the wrong way to pay for it. If the second read is to stay, `await asyncio.to_thread(...)` is mandatory.

---

### LOW-1 — `_extract_playlist_id` only inspects the first `list=` query value; URLs with multiple `list=` params silently lose later ones

- **Severity:** LOW
- **Where:** `src/ryzic/playlist_cache.py:169-179` (`_extract_playlist_id`)
- **Why it matters:** `parse_qs` returns a list when a key repeats. `values[0]` takes the first; everything else is discarded. For a URL like `?list=../../etc&list=PLgoodgoodgood`, the first value fails the regex and the function returns `None`, which raises `FetchFailed` — the safer default. But the underlying behavior ("first wins, rest ignored") is undocumented and the test for the adversarial path (`test_fetch_with_fallback_reraises_when_list_param_invalid`) doesn't exercise the multi-value case. yt-dlp's own URL parser may pick a different `list=` value (typically the last one) when handed the same URL, so cache-key drift is theoretically possible if YouTube ever serves redirects with stacked `list=` params.
- **Fix:** Either (a) document the "first `list=` wins" contract in the docstring, or (b) match yt-dlp's actual choice (usually `parsed.query`'s last `list=` for a duplicate key). A one-line test asserting current behavior locks the contract: `?list=BAD&list=PL_valid_______` returns `None` (i.e. fail-closed when ANY `list=` is bad).

---

### LOW-2 — No `relative_to(cache_root)` defense-in-depth on the derived path

- **Severity:** LOW
- **Where:** `src/ryzic/playlist_cache.py:47-50` (`_path_for`)
- **Why it matters:** Plan §4 (audio cache) prescribes regex-validate-then-`relative_to(cache_root).resolve()` as the path-safety pattern, and `ytdlp.download` (PR3a) implements the `relative_to` check in code. Plan §5 (this PR) doesn't make the second step explicit, but the same threat model applies. The regex `^[A-Za-z0-9_-]{10,50}$` is exhaustive enough that no in-band character can construct a traversal — `.`, `/`, NUL are all rejected — so the missing belt-and-suspenders check is theoretical. Still, if the regex ever loosens (e.g. someone widens it to allow `.` for a future YouTube format), there's no second wall. The audio-cache PR has the second wall by design.
- **Fix:** Add a one-liner after path construction:
  ```python
  derived = cache_root / _PLAYLISTS_DIR / f"{playlist_id}.json"
  derived.resolve().relative_to(cache_root.resolve())  # raises ValueError → wrap in InvalidVideoID
  return derived
  ```
  Or accept the asymmetry and add a comment to `_path_for` noting "regex is exhaustive; no `relative_to` second wall is needed — change this if widening the charset". Either is fine; silence is the worst option because the next reviewer will wonder.

---

### LOW-3 — `InvalidVideoID` raised for `playlist_id` is lossy naming

- **Severity:** LOW
- **Where:** `src/ryzic/playlist_cache.py:42-44, 138-139` and `src/ryzic/errors.py:13-14`
- **Why it matters:** The exception class docstring says "A video ID failed character-set or path-safety validation." Reusing it for playlist-ID failures means any logged exception, debugger trace, or future caller `except InvalidVideoID:` handler can't tell which kind of input failed. The PR's own message strings disambiguate (`"playlist_id failed validation: ..."`, `"playlist_id mismatch: ..."`), but the type does not. `M1-simplify.md` §10 keeps a two-exception roster (`FetchFailed`, `InvalidVideoID`) deliberately, so this is a defensible reuse — but the class name lies about its scope.
- **Fix (pick one):**
  1. Rename the class to `InvalidIdentifier` (or `InvalidCacheKey`) and update the docstring + the one PR3a usage. Two-call-site rename, low blast radius. Keeps the two-exception roster.
  2. Leave the class as-is and update its docstring to `"A cache-key identifier (video_id or playlist_id) failed character-set or path-safety validation."` Cheapest fix, preserves type hierarchy.

  My recommendation: option 2 — the name is regrettable but cheap to live with at this stage; the comment fix removes the false advertising and leaves a refactor trail for later.

---

### LOW-4 — `is_stale` accepts a `PlaylistInfo` whose `playlist_id` may itself fail validation; the resulting `InvalidVideoID` is leaked

- **Severity:** LOW
- **Where:** `src/ryzic/playlist_cache.py:156` (`_path_for(info.playlist_id, cache_root)`)
- **Why it matters:** `is_stale` does not catch the `InvalidVideoID` that `_path_for` raises if `info.playlist_id` is malformed (e.g. a deserialization-skipped check, or a `PlaylistInfo` constructed by the caller for some other purpose). The other failure modes (missing file, bad JSON, missing `fetched_at`) are normalized to `return True` per the docstring's "safer default when staleness can't be proven fresh" promise, but a bad ID raises out — inconsistent with the rest of the function.

  This is a thin edge in M1 (the only producers of `PlaylistInfo` are `resolve_playlist` and `_deserialize`, both of which validate); the test suite doesn't cover it. It will become a real footgun once a future caller hand-builds a `PlaylistInfo` for testing or a feature like "rename" lands.
- **Fix:** Wrap the `_path_for` call in `try/except InvalidVideoID: return True`. One line, matches the documented contract. Add a parametrized test variant.

---

### LOW-5 — `_write_sync` tempfile is not cleaned up on partial failure (mid-write crash leaves `*.json.tmp`)

- **Severity:** LOW
- **Where:** `src/ryzic/playlist_cache.py:97-107` (`_write_sync`)
- **Why it matters:** If `tmp.write_text(...)` succeeds but `tmp.replace(path)` fails (rare — disk full mid-rename, EROFS toggling, etc.), or if the process is killed between them, the `*.json.tmp` file lingers in `playlists/`. There's no orphan sweep for the playlist cache (audio_cache has one in its design). On a cluttered cache dir, this creates eventual disk-leak noise. Failure modes are narrow and the file is small (kilobytes per playlist); not urgent. The audio-cache spec calls out the same risk and addresses it via startup orphan sweep.
- **Fix:** Either (a) accept the leak (defensible — JSON files are small and the cache dir is bot-owned), or (b) add a `try/finally: tmp.unlink(missing_ok=True)` around the `replace` call. Option (b) is two lines and zero behavioral cost on the happy path; recommend it.

---

### NIT-1 — `read` and `is_stale` have overlapping responsibilities; the `fetched_at` round-trip is the symptom

See MEDIUM-1's recommended fix (option b) — returning the timestamp from `read()` collapses the symmetry helper duplication and makes `is_stale` a synchronous predicate over data the caller already holds. This is the cleanest SRP outcome, but it changes the public API shape (`read` now returns `tuple[PlaylistInfo, int] | None` or similar). Defensible to defer to a follow-up if MEDIUM-1 is fixed via the simpler `asyncio.to_thread` route.

---

### NIT-2 — `_PLAYLISTS_DIR` constant is single-use; a `Final` string for one path segment is over-modeled

- **Severity:** NIT
- **Where:** `src/ryzic/playlist_cache.py:39`
- **Why it matters:** The constant is referenced exactly once (in `_path_for`). Per `M1-simplify.md` §11's locality-of-reference principle, inlining `"playlists"` in the only place it's used is more readable than the indirection. Counter-argument: keeping it as a `Final` documents the on-disk layout in one place. Both are defensible; minor.
- **Fix:** Either inline it, or add a one-line comment explaining why it's hoisted (e.g. "// Layout per M1 §4 storage diagram"). Drop-in at maintainer's discretion.

---

### NIT-3 — `_deserialize` accepts numerically-coerceable `duration_ms` strings (e.g. `"213000"`)

- **Severity:** NIT
- **Where:** `src/ryzic/playlist_cache.py:82` (`duration_ms=int(e["duration_ms"])`)
- **Why it matters:** `int("213000")` succeeds; `int("213.0")` raises `ValueError` and is correctly caught. `str(...)` calls on the string fields (`video_id`, `url`, `title`, `uploader`) similarly tolerate non-string JSON values by stringifying them. Defense-in-depth against payload drift, but slightly more permissive than "exact JSON shape per spec". Not a security risk — payloads are bot-written. Document the latitude or tighten to `isinstance` checks; either is fine. The current behavior is the more useful one in practice (handles a yt-dlp version bump that flips a field type without crashing the cache).

---

### Validation summary against the brief's seven review prongs

1. **Correctness vs plan §5** — Signatures match within the documented `is_stale` exception (kwarg `cache_root` for the disk-read rationale; PR description justifies the deviation). JSON shape includes the four spec'd fields plus `fetched_at`. Regex `^[A-Za-z0-9_-]{10,50}$` enforced before path join in `_path_for`. Path safety: regex is exhaustive; no `relative_to(cache_root)` second wall (LOW-2).
2. **Lookup flow** — Live-first, write on success, read on `FetchFailed`, return regardless of TTL. Confirmed in `fetch_with_fallback` and locked in by `test_fetch_with_fallback_returns_stale_cache_unconditionally`. `is_stale` is decoupled from the fallback decision — correct per spec.
3. **Module functions only, no class** — Confirmed. Five public surfaces (`read`, `write`, `is_stale`, `fetch_with_fallback`, plus the implicit `PlaylistInfo` re-export via direct import). No class.
4. **`InvalidVideoID` reuse** — Defensible per the simplify decision (LOW-3); name lies about scope; cheap docstring fix.
5. **Atomic write** — Earned. Two `/play` calls for the same playlist URL while a download is in progress (rare but possible) would race a non-atomic `write_text`. The cost is one tempfile + one rename — trivial. Correct call.
6. **Tests** — All six brief-requested cases covered:
   - Round-trip (incl. unicode): `test_round_trip_preserves_playlist`, `test_round_trip_preserves_unicode`.
   - Regex rejection: `test_validate_rejects_invalid_on_{read,write}` parametrized.
   - Fallback flow when yt-dlp raises: `test_fetch_with_fallback_uses_cache_on_yt_dlp_failure`, `test_fetch_with_fallback_reraises_when_no_cache`, `test_fetch_with_fallback_returns_stale_cache_unconditionally`.
   - `is_stale` boundary: `test_is_stale_just_under_24h_is_fresh`, `test_is_stale_exactly_24h_is_fresh`, `test_is_stale_just_over_24h_is_stale`.
   - Malformed JSON: `test_read_malformed_returns_none`, `test_read_structurally_invalid_returns_none`.
   - Plus path-traversal defense: `test_traversal_id_does_not_create_file_outside_cache`.
   - Plus non-`FetchFailed` propagation: `test_fetch_with_fallback_propagates_unexpected_exceptions`.
   Coverage is high — no obvious untested branches except the LOW-4 case (bad `playlist_id` on `is_stale`).
7. **Comments** — Module docstring and function docstrings explain WHY (especially the `is_stale` deviation, `fetch_with_fallback`'s "unsinkable" rationale, the atomic-write contract). One narrative comment at line 31-34 (the `_PLAYLIST_ID_RE` constant) does narrate WHY (path-safety motivation) — appropriate. No WHAT-narration comments spotted.

### Watchlist (out of scope but worth tracking)

- **W1 — sync I/O in async modules.** The `is_stale` precedent (MEDIUM-1) is the only sync disk-touch in this module. As `audio_cache.py` lands, watch for the same anti-pattern in any predicate-shaped helper.
- **W2 — Two-exception roster strain.** This PR is the second consumer of `InvalidVideoID` for non-video-id semantics. If `audio_cache.py` adds a third (e.g. for cache-key validation on a non-video artifact), it's time to widen the name (LOW-3 option 1).
- **W3 — Playlist-cache orphan sweep.** None today. If `*.json.tmp` files start accumulating under `playlists/` in production, retroactively add a startup sweep mirroring audio_cache's orphan-clear logic.
- **W4 — `is_stale` boundary on system clock skew.** Strict `>` over a wall-clock subtraction is correct per spec wording, but if the host clock jumps backward (NTP correction), a freshly-written cache can read as stale. Acceptable for "embed warning" semantics; if `is_stale` ever gates the fallback decision, revisit.

---

## Verdict

**Minor revisions.**

One real blocker (MEDIUM-1: `is_stale` blocks the event loop on a sync `read_text` from an async call path) plus a handful of polish items (LOW-1 through LOW-5). The MEDIUM is a straightforward `async def` + `asyncio.to_thread` change — five-line fix, no API impact. The plan-conformance, test coverage, and security posture are otherwise solid; the deviations from plan §5 (kwarg on `is_stale`, atomic write, fetch_with_fallback wrapper) are all earned and well-defended in the PR description.

Recommend: fix MEDIUM-1, accept LOW-2 with a docstring note (or implement the one-liner), apply LOW-3 docstring fix, defer LOW-1/LOW-4/LOW-5 to follow-ups (or batch them in if the implementer wants the full sweep). Then ship.
