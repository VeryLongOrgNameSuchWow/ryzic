# PR #2 Simplification Pass — `feat(ytdlp): wrapper + URL validator + tests`

**Branch:** `feat/ytdlp-and-url-validator` -> `main`
**Diff reviewed:** +774 LOC across 4 files (250 LOC src, 524 LOC tests).
**Companion docs:** `docs/plans/M1-simplify.md` (already-decided plan-level cuts), `docs/plans/M1.md` §6 (PR3a spec).

This pass looks only for code that's *more elaborate than its M1 §6 surface justifies*. It does **not** re-litigate decisions already locked in `M1-simplify.md` (flat module layout, `errors.py` central placement, no `YtDlpService` class). It does not critique correctness or security — those are other agents' lanes.

Honest framing: the source module is mostly tight, but it has one clear pattern of over-decomposition (a chain of single-call helpers around the `DownloadError` translation path). The test file is rich; ~80 LOC could come out without losing assertion coverage.

---

## Findings

### S-1 — Collapse the `_first_line` -> `_map_friendly` -> `_raise_from_download_error` chain into the one `except DownloadError` block

**What to cut/collapse:** Three single-call helpers (`_first_line`, `_map_friendly`, `_raise_from_download_error`) spread across 21 LOC, each used exactly once from the `except DownloadError` block in `_extract`. Inline the whole chain:

```python
except DownloadError as exc:
    detail = next(
        (s for s in (line.strip() for line in str(exc).splitlines()) if s),
        str(exc).strip(),
    )
    friendly = next((f for n, f in _FRIENDLY_ERROR_PATTERNS if n in detail), None)
    raise FetchFailed(friendly or detail) from exc
```

That eliminates the awkward `raise AssertionError("unreachable")  # pragma: no cover` line that `_raise_from_download_error` forces (rule 6 — defensive at internal boundary, present only because static analysis can't see through the helper).

**Where:**
- `src/ryzic/ytdlp.py:108-128` (`_first_line`, `_map_friendly`, `_raise_from_download_error`)
- `src/ryzic/ytdlp.py:208-212` (the unreachable-assert workaround in `_extract`)
- `tests/test_ytdlp.py:365-376` (the `test_first_line_extracts_first_non_empty` parametrized block)

**Why it's safe:** Every helper here is called from exactly one place. The "rule of three" mandates 3+ similar usages before extracting a helper; these are at one usage each. Inlining keeps the error-translation logic in one place where it actually fires (`_extract`), removes one source of "where does control flow when `_raise_from_download_error` returns?" confusion, and lets the type checker see that the `except DownloadError` block raises unconditionally — no `AssertionError("unreachable")` workaround needed.

The `_first_line` test goes away — its behaviour is already covered by `test_resolve_track_unknown_download_error_passes_first_line` and `test_resolve_track_maps_known_errors`, which exercise it through the public API. Internal-helper micro-tests (rule 11) duplicate the public-surface coverage.

If a future caller needs `_first_line` again (the M1 plan does not anticipate one), reintroduce it then.

**Estimated LOC saved:** ~30 LOC (21 src + 12 tests, minus ~3 LOC inlined back into `_extract`).

---

### S-2 — Drop the `_check_not_livestream` helper; keep `_is_livestream` and inline the raise

**What to cut/collapse:** `_check_not_livestream` (3 LOC) is a wrapper that exists only to combine `_is_livestream(info)` + `raise FetchFailed("livestream")`. It's called twice — once in `resolve_track`, once in `download` (the "defense-in-depth" post-check; see S-3). Inline both call sites:

```python
if _is_livestream(info):
    raise FetchFailed("livestream")
```

That's two lines vs. the helper-call's one. The win isn't LOC; it's removing one indirection layer from a hot read path. `_is_livestream` (the actual predicate) stays — it's used by `_reject_livestream_filter` too, so it earns the rule-of-three threshold.

**Where:** `src/ryzic/ytdlp.py:135-137` (the helper); `src/ryzic/ytdlp.py:226, 278` (call sites).

**Why it's safe:** Two-line raise idioms are a Python staple; wrapping them costs more in indirection than they save in LOC. Helpers/wrappers around yt-dlp logic that just thin-pass arguments (rule 1) — this one isn't quite thin-pass, but it's a glorified two-statement combo.

If S-3 lands (cutting one of the two callers), `_check_not_livestream` becomes a pure single-call helper and S-2 becomes a near-zero-thought inline.

**Estimated LOC saved:** ~3 LOC.

---

### S-3 — Pick one livestream guard for `download()`: `match_filter` OR post-extract check, not both

**What to cut/collapse:** `download()` currently sets `opts["match_filter"] = _reject_livestream_filter` AND calls `_check_not_livestream(info)` after. The author's own comment admits this is duplication: *"Defense-in-depth: `match_filter` should have aborted, but a post-check costs nothing and keeps the contract explicit."*

Pick one. **Recommended: drop the `match_filter` callback and keep the post-check.** Reasoning:

- The post-check uses the exact same `_is_livestream(info)` predicate as `resolve_track`, so the contract is symmetric: "all info dicts handed to a public function are livestream-checked at the same point." One mental model.
- `match_filter` is yt-dlp library indirection — when `_reject_livestream_filter` returns `"livestream"`, yt-dlp wraps it in a `RejectedVideoReached` -> `DownloadError` -> we then translate that back via `_raise_from_download_error`. Three layers of bouncing for what the post-check does in one. Test `test_download_rejects_livestream` covers exactly this path with the post-check alone.
- The "before bytes hit disk" justification is thin: `extract_info(download=True)` runs metadata extraction first and only starts writing after `match_filter` clears. The post-check sees the same info dict at the same point in time — there is no race.

That removes `_reject_livestream_filter` (5 LOC) and the test `test_reject_livestream_filter_*` pair (8 LOC).

**Where:**
- `src/ryzic/ytdlp.py:140-144` (`_reject_livestream_filter`)
- `src/ryzic/ytdlp.py:274` (`opts["match_filter"] = ...`)
- `src/ryzic/ytdlp.py:276-278` (the "Defense-in-depth" comment + check stays — but the comment loses the "match_filter should have aborted" framing)
- `tests/test_ytdlp.py:311` (the `assert opts["match_filter"] is ...` assertion)
- `tests/test_ytdlp.py:379-386` (both `test_reject_livestream_filter_*` tests)

**Why it's safe:** Defense-in-depth at internal boundaries is rule-6 territory: validate at system edges, not at every function call between trusted parties. Both checks here run against an info dict yt-dlp produced from the URL we already validated upstream. There is no system boundary between the match_filter callback and the post-check — both fire inside the same `_extract` call. Picking the simpler one is the right move.

This is also the single biggest "feels like duplication" item in the file.

**Estimated LOC saved:** ~14 LOC (5 src helper + 1 src wiring + 8 tests).

---

### S-4 — Inline `_coerce_duration_ms`

**What to cut/collapse:** `_coerce_duration_ms` is 4 lines (signature + docstring + 2 body lines), called twice. Inline as `int(float(raw or 0) * 1000)` at both call sites:

```python
duration_ms=int(float(info.get("duration") or 0) * 1000),
```

**Where:** `src/ryzic/ytdlp.py:147-151` (the helper); `:164, :184` (call sites).

**Why it's safe:** Two callers + a one-line body falls below the rule-of-three abstraction threshold. The inlined expression is short enough to read at a glance, and the meaning ("seconds to ms with a None-safe default") is obvious without naming it.

The `raw or 0` collapses the explicit `is None` branch — `0` and `0.0` map to `0 ms` either way, and yt-dlp doesn't surface other falsy non-None values for `duration` (no booleans, no strings).

Marginal call. Defensible to keep if you'd rather have the named helper for grep-ability. Leans cut.

**Estimated LOC saved:** ~3 LOC.

---

### S-5 — Drop `test_base_opts_does_not_leak_global_state`

**What to cut/collapse:** `tests/test_ytdlp.py:353-357` asserts that `_base_opts(tmp_path)` returns a fresh dict each call by mutating one and verifying the other is untouched.

**Where:** `tests/test_ytdlp.py:353-357`.

**Why it's safe:** This tests Python language semantics (a function returning a dict literal returns a new dict each call), not a behavioural contract of `_base_opts`. The function's body is one `return {...}` literal — there is no global state to leak. The only way this test would fail is if someone refactored `_base_opts` to return a module-level constant — at which point the security-critical-settings test would still pass and several other tests would break in clearer ways (e.g. `test_download_invokes_extract_with_outtmpl` mutates `opts["outtmpl"]`).

Test scaffolding richer than the surface it tests (rule 11) — an implementation-detail test for a behaviour the language already guarantees.

**Estimated LOC saved:** ~6 LOC (function + decorator + blank line above).

---

### S-6 — Parametrize the three `resolve_track` livestream tests

**What to cut/collapse:** `test_resolve_track_rejects_active_livestream`, `test_resolve_track_rejects_upcoming_livestream`, and `test_resolve_track_accepts_recorded_was_live` are three near-identical 6-LOC functions distinguished only by their `info` payload and pass/fail expectation. Collapse into a single parametrized test:

```python
@pytest.mark.parametrize(
    ("live_status", "is_live", "rejects"),
    [
        ("is_live", True, True),
        ("is_upcoming", False, True),
        ("was_live", False, False),  # downloadable VOD
    ],
)
async def test_resolve_track_livestream_handling(
    tmp_path: Path, live_status: str, is_live: bool, rejects: bool
) -> None:
    info = _track_info(is_live=is_live, live_status=live_status)
    with patch.object(ytdlp, "_sync_extract", return_value=info):
        if rejects:
            with pytest.raises(FetchFailed, match="livestream"):
                await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
        else:
            track = await ytdlp.resolve_track(TRACK_URL, cache_root=tmp_path)
            assert track.video_id == YTID
```

**Where:** `tests/test_ytdlp.py:112-136` (three tests, 25 LOC including blank lines).

**Why it's safe:** Per the user's PR brief item 11 ("Any tests that could be parameterised?") — yes, these. The three tests share the entire scaffold (mock setup, cache_root plumbing, exception-context shape) and differ only in two fields and the expected outcome. Parametrization makes the data-driven nature of the case matrix visible at a glance: "here are the three cases, here's what we do with each."

Mild downside: the test now branches on a bool, which is a small smell. If you find that distasteful, an alternative split is one parametrized test for the rejection cases (active + upcoming) and a separate single test for the `was_live` accept case — still 12 LOC under the current 25.

**Estimated LOC saved:** ~12 LOC.

---

### S-7 — Convert `_FRIENDLY_ERROR_PATTERNS` to a dict literal

**What to cut/collapse:** The patterns are stored as `tuple[tuple[str, str], ...]`. The substring iteration `for needle, friendly in _FRIENDLY_ERROR_PATTERNS` works just as well over `dict.items()`:

```python
_FRIENDLY_ERRORS: Final[dict[str, str]] = {
    "Sign in to confirm your age": "age-restricted",
    "Private video": "private video",
    "Video unavailable": "region-blocked or unavailable",
}
```

The `Final` type changes from `Final[tuple[tuple[str, str], ...]]` to `Final[dict[str, str]]` — shorter and more idiomatic.

**Where:** `src/ryzic/ytdlp.py:46-54` (and the iteration site, which moves into the inlined block from S-1).

**Why it's safe:** Rule 2 — error mapping that could be a dict literal. The tuple-of-tuples shape costs a type annotation that's twice as long for no semantic gain. Order-of-iteration doesn't matter (the comment on line 49 even calls this out: *"Order doesn't matter — each pattern is unique enough to discriminate."*) — a dict's insertion-ordered iteration matches what the tuple does. If two patterns ever overlap, the comment becomes a lie either way.

Pure mechanical cleanup; pair with S-1 since both touch the same lines.

**Estimated LOC saved:** ~0 LOC (same line count, slightly shorter type annotation).

---

## Things I considered cutting and decided not to

### `validate_video_id` exposed publicly (PR brief item 7)
**Keep public.** PR brief asked: "exposed publicly when it's only used internally?" Answer: it's NOT only internal. M1 §4 *"Path safety"* explicitly says video IDs are validated *before path construction* — that happens in the audio cache layer (PR3b, separate module). Keeping `validate_video_id` public lets the cache layer pre-validate an ID before constructing a `Path`, exactly as the M1 plan calls out. The module docstring already explains this. Recutting now means re-introducing in PR3b with no net win.

### `_entry_from_flat` returns-None-on-invalid pattern (PR brief item 10)
**Keep.** PR brief asked: "clean or muddled?" My read: clean. The function is a filter-shaped predicate-and-construct: "given a possibly-bad entry, hand back a `TrackInfo` or skip it." The alternative — raising and try/except'ing in the comprehension — is uglier and slower. The call site `[t for t in (_entry_from_flat(e) for e in raw if isinstance(e, dict)) if t is not None]` is dense but each filter has a distinct job (skip non-dicts, then skip unusable entries). Per M1 §3 the playlist embed surfaces partial-failure counts rather than aborting — None-on-skip directly supports that.

The one nit: the inner `isinstance(e, dict)` filter could move *into* `_entry_from_flat` (cast `entry: Any` on the signature), reducing the comprehension to `[t for t in map(_entry_from_flat, raw) if t is not None]`. Mild simplification at the call site, mild loss of type safety on the helper. I'd leave it.

### `TrackInfo` / `PlaylistInfo` as frozen dataclasses (PR brief item 5)
**Keep.** PR brief asked: "could be a tuple? could be None?" Both have real shape: `TrackInfo` has 5 named fields with mixed types (str, str, str, str, int), `PlaylistInfo` has 3 including a list of `TrackInfo`. A `NamedTuple` would also work but doesn't beat `@dataclass(frozen=True)` for readability or mutation safety. A bare tuple loses the field names that callers (cache layer, embed builders, queue) will access by name. None of these would simplify.

### `_track_from_info` vs `_entry_from_flat` (similar but separate)
**Keep separate.** They look duplicative but the differences are real and load-bearing: `_track_from_info` raises on missing/invalid id (full extraction MUST yield an id); `_entry_from_flat` returns None (partial-failure tolerance per M1 §3); they prefer different URL fields (`webpage_url` vs `url`), and `_entry_from_flat` synthesizes a fallback URL from the id. Folding them into one function with a `raise_on_error: bool` flag would replace clean two-purpose helpers with a parameterized abstraction (rule 4 — flag arguments are anti-patterns when they switch behaviour). Three similar lines is better than a premature abstraction; ten similar lines with three real branches is *also* better than one parameterized helper.

### `validate_video_id` separate-function form (vs. inlining the regex)
**Keep.** Used in `_track_from_info`, `_entry_from_flat`, exposed publicly for the cache layer, and tested directly. Three call sites = above the rule-of-three threshold. The named function also documents the contract better than `_VIDEO_ID_RE.match(...)` at every call.

### `_sync_extract` and `_extract` as separate functions
**Keep both.** `_sync_extract` is the work the worker thread does; `_extract` is the async-and-error-normalize wrapper. Splitting them means the thread runs only the yt-dlp call (correct — no asyncio code in the worker), and the async layer can wrap exceptions cleanly. Inlining `_sync_extract` into `_extract` would put the `with YoutubeDL(...) as ydl: ...` block inside an `asyncio.to_thread(lambda: ...)` lambda — uglier and harder to mock.

### `url_validator.py` (entire file)
**Keep as-is.** 30 LOC including the docstring; one allowlist + one function. Module docstring explains *why* (regex check rejected upstream — review HIGH-1). `frozenset` is the right type. `is_supported_url` is one line. No fat to trim.

### `test_url_validator.py` (entire file)
**Keep as-is.** 80 LOC, fully parametrized, three groups (accepted, rejected, malformed) plus four edge-case singletons (urlparse `ValueError`, userinfo smuggling, case-insensitivity, frozenset immutability). Each singleton documents a non-obvious property that a parametrized list would obscure with a one-line case ID. The `test_allowed_hosts_is_immutable` could arguably go (tests language semantics), but it's three lines and pins a security contract — keep.

### `test_base_opts_security_critical_settings`
**Keep.** Pins every security-critical default in §6 against accidental change. Rule 11 might suggest "rich tests for a config dict" but every assertion here is a documented HIGH-priority security item — `cookiefile=None` (item 13), `geo_bypass=False`, `max_filesize` (item 5), `concurrent_fragment_downloads` (item 4 race), the codec allowlist (review §6 LOAD_FAILED). One test, one assertion per security concern, perfectly proportioned.

### Comments narrating WHAT (rule 9)
**Mostly absent.** I scanned every comment in the diff and they're all WHY-style — the cookies-disabled comment (security item 13), the format-priority comment (Lavaplayer codec constraint), the partial-failure comment (M1 §3 reference), etc. The one borderline case is the docstring on `_first_line` ("Return the first non-empty line ... defensively trimmed") which narrates WHAT — but S-1 deletes this helper anyway, so moot.

### `extract_flat` opts-override pattern in `resolve_playlist`
**Keep.** Three lines mutating the dict from `_base_opts(...)` — `opts["noplaylist"] = False; opts["extract_flat"] = True`. Could be `_base_opts(cache_root) | {"noplaylist": False, "extract_flat": True}`. Same LOC, same readability. Status quo is fine.

### `resolve_track` / `resolve_playlist` / `download` as three module-level functions
**Keep.** The M1-simplify decision (§10) explicitly rejected a `YtDlpService` class. Three functions are the right shape. None of them thin-pass to `_extract`; each has its own setup (opts mutation, dest sandbox check) and post-processing.

---

## Top 3 wins

1. **S-1: Collapse the error-translation helper chain (~30 LOC)** — Three single-call helpers + their unreachable-assert workaround + a parametrized test for one of them, all replaced by ~3 lines inside `except DownloadError`. Largest single cut; the inlined version is more readable because the entire DownloadError translation path lives in one place.
2. **S-3: Pick one livestream guard for `download()` (~14 LOC)** — Drops `match_filter` callback + its tests, keeps the post-check. Removes the file's only acknowledged "defense-in-depth at an internal boundary" smell.
3. **S-6: Parametrize the three livestream-handling `resolve_track` tests (~12 LOC)** — Three near-identical tests collapse into one parametrized case matrix that makes the live/upcoming/was_live decision table visible at a glance.

## Total LOC the PR could lose

- **Solid wins (S-1, S-2, S-3, S-5, S-6, S-7): ~65 LOC** (out of 774 total, or ~8%).
- **Including S-4 optional: ~68 LOC.**

Source side: ~28 LOC trimmed from `ytdlp.py` (250 -> ~222). Test side: ~38 LOC trimmed (524 -> ~486), still well above the security-critical assertions.

Headline: this PR is mostly tight, but the `DownloadError` translation chain and the `download()` defense-in-depth pair are the two real fat patterns. The rest of the cuts are sub-15-LOC trims around them. The URL validator and its tests are pristine — no recommendations there.
