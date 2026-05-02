# PR #5 Simplification Pass — `feat(cache): playlist metadata cache`

**Branch:** `feat/playlist-cache` -> `main`
**Diff reviewed:** +564 LOC across 2 files (`src/ryzic/playlist_cache.py` 209 LOC, `tests/test_playlist_cache.py` 355 LOC).
**Companion docs:** `docs/plans/M1-simplify.md` (locked plan-level cuts), `docs/plans/M1.md` §5 (PR4 spec — PR5 implements the same), prior `PR3-simplify.md` / `PR4-simplify.md` for tone calibration.

This pass looks only for shapes that are more elaborate than M1 §5 justifies. It does **not** re-litigate decisions in `M1-simplify.md` (24h hardcoded TTL, no env var, module functions instead of class, no sidecars). It does not critique correctness or security — those are other agents' lanes.

Honest framing: PR5 has a real bloat axis the brief correctly identified — the `is_stale` second-read and the test-side `_write_with_fetched_at` helper that hand-rolls a parallel serializer. Several smaller WHAT-narrating comments and three almost-identical boundary tests round it out. Realistic floor: **~35–55 LOC of source + ~25–35 LOC of tests**, ~12% of the PR. Nothing structural; no surface contract changes.

---

## Findings

### S-1 — Return `(info, fetched_at)` from `read()`; let `is_stale` take a timestamp

**What to cut/collapse:** `is_stale(info, *, cache_root)` does its own disk read just to recover `fetched_at`. The caller (`fetch_with_fallback` cache-hit branch + `/play`'s embed builder) has *already* called `read()` to get the `info` — two reads per fallback path, of which the second exists only because `fetched_at` got dropped on deserialization.

Reshape `read()` to return both: `tuple[PlaylistInfo, int] | None` (or a tiny `CachedPlaylist` NamedTuple if the call sites read better with a name). Reshape `is_stale` to a 1-line function that takes the timestamp directly:

```python
async def read(playlist_id: str, cache_root: Path) -> tuple[PlaylistInfo, int] | None: ...

def is_stale(fetched_at: int) -> bool:
    return (time.time() - fetched_at) > _TTL_SECONDS
```

`fetch_with_fallback`'s cache branch becomes:
```python
cached = await read(playlist_id, cache_root)
if cached is None:
    raise
info, fetched_at = cached
...
return info, fetched_at, True   # or change tuple shape; see S-2
```

**Where:** `src/ryzic/playlist_cache.py` lines 110–128 (`read`), 142–164 (`is_stale`); ripples into `fetch_with_fallback` (lines 188–209) and the `is_stale_*` tests (lines 209–247).

**Why safe:** Today the on-disk state IS the source of truth — `is_stale` reads it back precisely because it doesn't trust the in-memory `info`. But once `read()` has returned, the on-disk `fetched_at` is *frozen* relative to that snapshot — a concurrent overwrite would yield a strictly newer timestamp, which can only flip a "stale" answer to "fresh" (and the embed warning is the conservative side anyway). Passing the value the caller already paid I/O to read eliminates the second I/O without changing the behavior the embed sees. The brief's framing — "the kwarg requires a disk read" — is exactly the right diagnosis: the kwarg exists *because* the function fundamentally wants the timestamp, and the dance to recover it is the smell.

This is the single biggest signal-to-noise win in the PR. It also shrinks the `is_stale` docstring (the "missing file → stale" branch and the docstring's whole "we don't store fetched_at on the dataclass because that would leak the cache concern" paragraph both go away — once the timestamp travels alongside the info as a tuple, neither concern applies). The `is_stale_missing_*` tests become unreachable by construction (you can't ask "is this stale" without having read first), so they go away too.

**Estimated LOC saved:** ~15 LOC src (`is_stale` body shrinks from ~22 to ~2 lines, the second `_read_sync` call disappears, the `_PLAYLISTS_DIR`-derived path computation in `is_stale` disappears) + ~25 LOC tests (the two `is_stale_missing_*` tests + the per-boundary `_write_with_fetched_at` setup gets collapsed — see S-7).

---

### S-2 — Tuple `(info, used_cache)` is fine; do **not** introduce a `FetchResult` dataclass

**What to cut/collapse:** Nothing. Considered: replacing `tuple[PlaylistInfo, bool]` with a `@dataclass FetchResult(info: PlaylistInfo, is_fallback: bool)` (or NamedTuple) for self-documenting call sites.

**Why I'm not recommending it:** The brief asks "could it return one type with an `is_fallback` field?" — the honest answer is yes-but-don't. The tuple has exactly one consumer (`/play` in PR6a) and two cardinality, returned at the end of one function. A 2-tuple destructured as `info, used_cache = await fetch_with_fallback(...)` is the canonical Python shape; a `FetchResult` adds a class import to the call site and a `result.info`/`result.is_fallback` ceremony for negative wins. Per "three similar lines vs premature abstraction" — there isn't even one similar line here.

If S-1 lands, the natural shape becomes `tuple[PlaylistInfo, int, bool]` (info, fetched_at, used_cache_fallback) — at three elements I'd reconsider, but a 1-line NamedTuple `class CachedPlaylist(NamedTuple): info; fetched_at: int; used_fallback: bool` is the right answer there, NOT a dataclass. Defer to PR6a where the call site shape is observable.

**Estimated LOC saved:** 0. **Recommend skipping.** Tuple is appropriately minimal for its current cardinality and consumer count.

---

### S-3 — Atomic write via tempfile + `os.replace` — keep, but trim the docstring

**What to cut/collapse:** The atomic-replace machinery itself (lines 96–106) is ~10 lines and earned: one playlist cache file is read by `read()` concurrently with a `write()` for the same playlist (a `/play` retry while the previous fetch's writeback is in flight is a real race even on a single bot instance). `os.replace` is the stdlib idiom; no simpler shape gets the same property.

But the 4-line docstring (lines 98–102) narrates **what** the next 3 lines do (`mkdir`, write to tmp, replace) rather than **why** the atomicity matters in this codebase. Trim to:

```python
def _write_sync(path: Path, payload: dict[str, Any]) -> None:
    # Atomic replace: a concurrent reader sees either the prior version
    # or the new one, never a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
```

**Where:** `src/ryzic/playlist_cache.py` lines 97–102.

**Why safe:** Convert the docstring (formal API doc on a `_private` helper with one caller) to a comment that names the WHY in one sentence. The mechanics are stdlib-canonical and self-evident; the WHY (why bother for a per-playlist file) is the only part that needs words.

**Estimated LOC saved:** ~3 LOC.

---

### S-4 — `_serialize` / `_deserialize` symmetry — keep; **do not** swap to Pydantic

**What to cut/collapse:** Nothing. Considered the brief's framing: "could `model_dump_json` / `model_validate_json` cover it?"

**Why I'm not recommending it:** Pydantic isn't a project dependency (M1 §1 lists only `aiosqlite` as new) and adding it to satisfy two helpers is a bad trade — Pydantic on the dependency floor pulls in 8MB and a transitive surface that nothing else in the bot uses. The dataclasses in `ytdlp.py` are intentionally thin (`@dataclass(frozen=True)` mirroring yt-dlp's shape — `M1-simplify.md` §10).

What about `dataclasses.asdict` + a `**dict` constructor for symmetry? `_serialize` already uses `asdict` — that side IS one-liner-clean. The asymmetry is on `_deserialize` because it needs structural validation: a malformed cache file must NOT crash the bot, and a naïve `PlaylistInfo(**payload)` would `TypeError`-storm on missing/wrong-typed keys. The current shape — explicit `isinstance` checks + per-field `str()`/`int()` coercion — is the validation, not boilerplate. The bot has zero schema-validation library on the dep floor; rolling 15 lines of structural checks is the right size.

A small WIN: `_serialize` is currently 7 lines including the wrapper dict — it could collapse to:
```python
def _serialize(info: PlaylistInfo, fetched_at: int) -> dict[str, Any]:
    return {**asdict(info), "fetched_at": fetched_at}
```
…**but** that drops the explicit ordering of keys (currently `playlist_id`, `title`, `fetched_at`, `entries`) and means the on-disk file shape becomes implicitly tied to `PlaylistInfo`'s field order. The current explicit construction is more robust to dataclass-field reordering. Net: leave it, **0 LOC saved**.

**Where:** `src/ryzic/playlist_cache.py` lines 53–86.

**Estimated LOC saved:** 0. **Recommend skipping.**

---

### S-5 — Defensive read: collapse one of two warning sites; keep the four exception classes

**What to cut/collapse:** The brief asks: "All four branches earned?" Looking at `read()` lines 110–128, the actual handling is a tree:

```python
try:
    payload = await asyncio.to_thread(_read_sync, path)
except (json.JSONDecodeError, OSError, UnicodeDecodeError):     # ← branch A
    _log.warning("dropping unreadable playlist cache entry: %s", path)
    return None
if payload is None:                                              # ← branch B (file not found)
    return None
try:
    return _deserialize(payload)
except (ValueError, KeyError, TypeError):                        # ← branch C
    _log.warning("dropping malformed playlist cache entry: %s", path)
    return None
```

Three branches, not four — and all three are earned for distinct reasons:
- **A** catches "file exists but is corrupted on disk" (truncated, bit-flipped, foreign-encoding, FS-level error).
- **B** is the common case (cache miss), silent + no log.
- **C** catches "JSON parsed but doesn't match our schema" (older code wrote a different shape, or the file was hand-edited).

A and C BOTH log "dropping … cache entry" with the same path. The **diagnostic difference** between "unreadable" and "malformed" is real — one means "FS or encoding broke," the other means "schema drifted." Worth keeping as separate log lines. **Don't collapse.**

The one tiny cleanup: the `_read_sync` helper currently catches only `FileNotFoundError` and lets `OSError`/`json.JSONDecodeError`/`UnicodeDecodeError` propagate to its async caller. That's fine — `_read_sync` is pure I/O, its caller is the policy layer. Leave it.

The smell, if any, is the exception list `(json.JSONDecodeError, OSError, UnicodeDecodeError)` — `json.JSONDecodeError` IS a `ValueError`, and `UnicodeDecodeError` IS a subclass of `ValueError`. So the exception tuple could shorten to `(ValueError, OSError)` and catch the same set. But: **explicit-by-name is more grep-able**. A future contributor reading `except (ValueError, OSError)` has to remember which `ValueError` subclasses come from where; the current spelling tells the story at the point of catch. Net: leave it, **0 LOC saved**.

**Where:** `src/ryzic/playlist_cache.py` lines 110–128.

**Estimated LOC saved:** 0. **Recommend skipping.** The branches earn their keep; the redundant log site reads as defense-in-depth, and explicit exception names are friendlier than the deduped form.

---

### S-6 — Trim WHAT-narrating comments and over-formal docstrings

**What to cut/collapse:** Several comments and docstrings narrate WHAT the next line does or restate the type signature:

- **Module docstring lines 1–12** (12 lines): the second paragraph ("The cache exists for one job: keep `/play <playlist_url>` working when yt-dlp breaks") and the TTL paragraph (lines 10–11) carry real WHY and stay. The first sentence ("Stores resolved PlaylistInfo snapshots as JSON files under …") narrates the type signature visible 30 lines below. Could trim to ~6 lines but that's bikeshed-territory; **leave it** — module docstrings are the right place for context.

- **`_path_for` line 47** docstring "Return the JSON path for `playlist_id` after validating the id." narrates the function name + the next two lines. Cut.

- **`_serialize` lines 53–60** has no docstring (good).

- **`_deserialize` lines 63–69** docstring's first sentence is intent. The `Raises:` paragraph narrates WHAT the next 15 lines visibly do. Cut the Raises paragraph; the caller catches them by name and the helper's `_private` underscore signals it's internal.

- **`_PLAYLIST_ID_RE` comment lines 32–35** — 4 lines of WHY (charset bound, path-safety contract). **Keep.** Real load-bearing intent.

- **`is_stale` docstring lines 142–151** — once S-1 lands, this whole docstring goes away (`is_stale(fetched_at: int) -> bool` is self-documenting). If S-1 is rejected: the "Reads `fetched_at` from disk so the function works on a freshly deserialized PlaylistInfo without storing the timestamp on the dataclass" paragraph is real WHY. The "missing file → stale" paragraph narrates a defensive default — keep that one line.

- **`fetch_with_fallback` docstring lines 195–204** — both paragraphs are real WHY (`used_cache_fallback` consumer, "unsinkable" floor). **Keep.**

- **`write()` docstring lines 132–137** — the WHY about the playlist_id mismatch IS real (silent breakage of next read). **Keep.**

- **`_extract_playlist_id` line 178** — one-liner docstring restates the function name. Cut.

**Where:** `src/ryzic/playlist_cache.py` lines 47, 63–69 (Raises paragraph), 142–151 (collapsed by S-1), 178.

**Why safe:** Docstrings on private helpers narrating their bodies are noise; the helpers' names + 5-line bodies tell the story at the point of call. WHY-comments stay; WHAT-restatement goes.

**Estimated LOC saved:** ~6 LOC (4 docstring lines + the `Raises:` paragraph + a couple of redundant one-liner docstrings).

---

### S-7 — Test boundary trio → one parameterized test; drop the `_write_with_fetched_at` shadow serializer

**What to cut/collapse:** Three separate test functions probe the same `is_stale` function at the 24h boundary (lines 209–232):

- `test_is_stale_just_under_24h_is_fresh` (`fetched_at = now - (24h - 1)` → `False`)
- `test_is_stale_exactly_24h_is_fresh` (`fetched_at = now - 24h` → `False`)
- `test_is_stale_just_over_24h_is_stale` (`fetched_at = now - (24h + 1)` → `True`)

Each is 7 lines of identical setup + 1-line assertion delta. Standard pytest parameterization:

```python
@pytest.mark.parametrize(
    ("offset_seconds", "expected_stale"),
    [
        (24 * 60 * 60 - 1, False),  # 23h59m59s
        (24 * 60 * 60,     False),  # exactly 24h (boundary is strict >)
        (24 * 60 * 60 + 1, True),   # 24h00m01s
    ],
)
def test_is_stale_boundary(tmp_path, offset_seconds, expected_stale):
    now = 1_700_000_000
    info = _write_with_fetched_at(tmp_path, now - offset_seconds)
    with patch.object(playlist_cache.time, "time", return_value=now):
        assert playlist_cache.is_stale(info, cache_root=tmp_path) is expected_stale
```

(If S-1 lands, this test mostly evaporates — the boundary check becomes a 4-line pure-function test with no `tmp_path` and no `_write_with_fetched_at` machinery at all.)

The `_write_with_fetched_at` helper itself (lines 186–207) is 22 lines that **hand-rolls a second copy of `_serialize`** — every field listed by hand, again, with the same shape constraint. If S-1 lands, this helper goes away. If S-1 is rejected, the helper could lean on the production serializer:

```python
def _write_with_fetched_at(tmp_path: Path, fetched_at: int) -> PlaylistInfo:
    info = _playlist()
    payload = playlist_cache._serialize(info, fetched_at=fetched_at)
    path = tmp_path / "playlists" / f"{info.playlist_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return info
```

(Reaches into a `_private` helper from tests, which is a code smell — but the smell is the *symptom* of needing to override `fetched_at`, which is itself the smell S-1 fixes. The cleanest endgame is S-1.)

**Where:** `tests/test_playlist_cache.py` lines 186–232.

**Why safe:** Three back-to-back tests with structural duplication is exactly what `parametrize` exists for; each parameter row's comment explains the boundary it pins. If S-1 lands, both the helper and the helper-using tests become trivially shorter.

**Estimated LOC saved:** ~10 LOC if S-7 alone (parameterize the trio, leave helper), ~30 LOC if S-1 + S-7 together (helper goes away entirely; the `is_stale_missing_*` tests go too).

---

### S-8 — Test `# ---` separator banners

**What to cut/collapse:** Three 3-line ASCII separator blocks visually section the test file (lines 43–45, 113–115, 181–183, 249–251). They're ~12 lines of decoration.

Replace with single `# region: round-trip` / `# region: validation` / `# region: is_stale` / `# region: fetch_with_fallback` comments (or just `# === <section> ===` 1-liners), or trust pytest's class-grouping. The banners restate what the next test names already say; the function names alone navigate fine.

**Where:** `tests/test_playlist_cache.py` lines 43–46, 113–116, 181–184, 249–252.

**Why safe:** Cosmetic; mirror the same finding in `PR3-simplify.md` S-10. Visual-separator banners are personal-style; in a file where every test name starts with `test_<area>_<scenario>`, they add no navigation value beyond the function names themselves.

**Estimated LOC saved:** ~9 LOC (3 lines per banner × 4 banners − 4× 1-line replacements).

---

### S-9 — Validation-rejection tests: dedupe the "rejects on read" + "rejects on write" parameter lists

**What to cut/collapse:** Two parameterized tests cover the same `_validate_playlist_id` rejection logic from two entry points (`read` line 133–149 vs `write` line 154–164). The `write` parameter list is a strict subset of the `read` list — it skips `"a" * 51`, `"abc.json"`, `"abc%20def"`, `"abc?def"`, `"abc/def"`, `"abc\x00def"`. The asymmetry isn't load-bearing; both functions pass through `_path_for` → `_validate_playlist_id` and reject identically. Either:

(a) **Test the validator directly**: one `@pytest.mark.parametrize` block over the bad-id list against `playlist_cache._validate_playlist_id` (or `_path_for` if `_validate_playlist_id` is too internal). Drop the `read`/`write` integration variants entirely; the integration is one line of pass-through that doesn't need re-pinning at every entry point.

(b) **Keep one integration variant** (say, on `read`, since it's the read-only path) + drop the `write` duplicate. Simpler at the cost of slightly weaker coverage at the `write` entry.

Plus `test_traversal_id_does_not_create_file_outside_cache` (lines 170–178) is a defense-in-depth assertion that overlaps `test_validate_rejects_invalid_on_write` for the `"../escape"` case — its real value is the "nothing leaked into the parent" tail check. If we go with (a), keep this one as the single integration-level smoke for the path-safety story; otherwise it's redundant with the parameterized `write` rejection.

**Where:** `tests/test_playlist_cache.py` lines 133–178.

**Why safe:** Two parameterized blocks testing the same charset against two functions that both call the same validator is over-specification. The validator's contract is the right unit; the two callers are integration smoke at most. Mirrors the maintainer's preference (per `M1-simplify.md` review tone) for testing units of behavior, not 2× the same unit through two different doors.

**Estimated LOC saved:** ~12 LOC (drop the `write`-side parameterized list + collapse traversal test into the validator-level test, OR drop the `write`-side and keep the traversal smoke; either path lands ~10–14 LOC under).

---

### S-10 — Collapse two redundant fetch_with_fallback re-raise tests

**What to cut/collapse:** `test_fetch_with_fallback_reraises_when_url_has_no_list_param` (lines 318–329) and `test_fetch_with_fallback_reraises_when_list_param_invalid` (lines 332–342) both pin the same property: `_extract_playlist_id` returns `None` → reraise. Different inputs (no `list=` vs invalid `list=`), same assertion path.

```python
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # no list= param
        "https://www.youtube.com/playlist?list=../../etc",  # adversarial list=
    ],
)
async def test_fetch_with_fallback_reraises_when_no_extractable_id(tmp_path, url):
    with (
        patch.object(playlist_cache, "resolve_playlist", side_effect=FetchFailed("x")),
        pytest.raises(FetchFailed),
    ):
        await playlist_cache.fetch_with_fallback(url, cache_root=tmp_path)
```

The `await playlist_cache.write(PLIST_ID, _playlist(), tmp_path)` setup in `test_fetch_with_fallback_reraises_when_url_has_no_list_param` (line 322) is doing extra work to assert "even with an unrelated cache file present, no list= → no fallback." That negative-control adds value; keep it as a comment in the parameter row, OR keep it as a third parameter case where `cache_pre_populated=True`.

**Where:** `tests/test_playlist_cache.py` lines 318–342.

**Why safe:** Same mechanism, two inputs → parametrize. Pytest output still names the failing input via the parameter id. The "unrelated cache exists" negative control is a single `pre_populate=True` parameter, not a whole separate test function.

**Estimated LOC saved:** ~10 LOC.

---

### S-11 — `_extract_playlist_id` swallows `urlparse` `ValueError` — drop the try/except

**What to cut/collapse:** `_extract_playlist_id` (lines 178–187) wraps `urlparse(url)` in `try/except ValueError`. `urllib.parse.urlparse` is documented to be very permissive — it handles malformed URLs by returning empty components rather than raising. The `ValueError` branch fires only for IPv6 zone-id parsing pathologies (`urlparse("http://[::1%foo]")` and similar), which `_extract_playlist_id`'s callers will never see. The `parse_qs` of the `query` attribute is similarly tolerant.

```python
def _extract_playlist_id(url: str) -> str | None:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("list")
    if not values:
        return None
    candidate = values[0]
    return candidate if _PLAYLIST_ID_RE.match(candidate) else None
```

**Where:** `src/ryzic/playlist_cache.py` lines 179–183.

**Why safe:** The entry point is `fetch_with_fallback`, which only sees URLs that just survived `resolve_playlist` enough to fail at the **download** stage (not URL-parse stage). By that point, the URL has already passed through `is_supported_url` (`url_validator.py`) and `urlparse` once already without raising. A defensive wrapper around `urlparse` that's never tripped is dead branch.

If this pattern recurs elsewhere in the codebase, it's worth a project-wide note. Standalone in this PR, it's a 3-line trim.

**Estimated LOC saved:** ~3 LOC.

---

### S-12 — `_PLAYLISTS_DIR: Final = "playlists"`

**What to cut/collapse:** A `Final` constant for one-string-used-twice (lines 36 + 49 in `_path_for`, plus references in tests). The string `"playlists"` appears in:
- `playlist_cache.py` line 49 (the `_path_for` join)
- `tests/test_playlist_cache.py` lines 71, 78, 85, 105, 204, 240, 290, 298 — 8 sites.

Tests can't import a `_private` constant cleanly anyway (and ~half do hardcode the string), so the `Final` constant is a half-measure. Two viable shapes:

(a) **Inline the string** at the one production site (`cache_root / "playlists" / f"{playlist_id}.json"`) and accept the tests hardcoding it — they're the only other 8 sites, and `tmp_path / "playlists" / f"{PLIST_ID}.json"` is more readable than `tmp_path / playlist_cache._PLAYLISTS_DIR / f"{PLIST_ID}.json"` anyway.

(b) **Keep the `Final`** and stop here.

(a) is one fewer named thing in the module. The constant earns its keep only if "playlists" is going to change (it isn't — it's the spec'd directory name from M1 §4 storage layout).

**Where:** `src/ryzic/playlist_cache.py` lines 36, 49.

**Why safe:** The string appears once in production code; the `Final` declaration is more characters than the use. Mirrors the project's preference for inline literals (e.g. `audio_cache` likely doesn't `Final`-name "audio" — verify in PR3b).

**Estimated LOC saved:** 1 LOC. **Optional, low-conviction.**

---

### S-13 — Things considered and kept

- **`_validate_playlist_id` as a separate function** — keep. Three callers (`_path_for`, `_extract_playlist_id`, `write`'s mismatch check via `_path_for`), each with the same regex. This is the "≥3 callers" threshold; helper earns it.
- **The `playlists/` subdirectory** — keep. Spec'd in M1 §4 storage layout (sits next to `audio/`, `index.sqlite`, `tmp/`). Don't second-guess.
- **`asyncio.to_thread(_read_sync, path)` for the *read* path** — keep. `read_text` is sync; running on the loop blocks. The brief doesn't ask, but this is cheap-correct, not over-engineered.
- **The `_log.warning` on dropped entries** — keep. Operationally critical; without it, a corrupted cache silently re-misses on every `/play` retry without anyone noticing.
- **`_extract_playlist_id` as a separate function (vs inlined into `fetch_with_fallback`)** — keep. The 8-line URL-parsing logic is a self-contained unit and tested at the integration level via `fetch_with_fallback_reraises_when_*`. Inlining would push the URL-parse decisions into the middle of the fallback flow.
- **`fetched_at = int(time.time())`** — keep as int (vs float). On-disk JSON is more diff-friendly with integer seconds; sub-second precision is wasted given the 24h staleness window.
- **The `info.playlist_id != playlist_id` mismatch guard in `write()`** — keep. The docstring's WHY is real: a mismatched payload would silently cache under the wrong filename and break the next read for both ids.
- **`_serialize` taking `fetched_at` as a parameter (vs reading `time.time()` internally)** — keep. Pure function; testability is the right shape.
- **Module docstring** — keep mostly. Slightly long but every paragraph carries non-obvious WHY (the live-first contract, the 24h-only-for-embed-not-fallback distinction). Mirrors the `PR3-simplify.md` "module docstring keep-as-is" verdict.

---

## Top 3 wins by impact

1. **S-1 — Return `(info, fetched_at)` from `read()`; reduce `is_stale` to a 1-line pure function.** ~15 LOC src + ~25 LOC tests = **~40 LOC**, eliminates a second disk read on the cache-hit fallback path, and dissolves the "we don't store fetched_at on the dataclass because that would leak the cache concern" architectural justification entirely (it stops being a concern once the timestamp travels separately). The brief identified this; it's the right call.

2. **S-7 + S-8 + S-10 — Test parameterization + banner trim.** Three is_stale boundary tests collapse to one parameterized test; two `fetch_with_fallback` re-raise tests collapse to one parameterized test; four ASCII separator banners become 1-line section markers. Together **~25–30 LOC** of test bloat without losing coverage. (Some of S-7's savings overlap with S-1; counted once below.)

3. **S-9 — Test the validator once, not twice via `read` + `write`.** ~12 LOC by testing `_validate_playlist_id` (or `_path_for`) directly against the bad-id list, instead of re-asserting the same rejection through both `read` and `write` integration paths.

---

## Keep as-is

- The atomic write via tempfile + `os.replace` (S-3 trims a docstring; the mechanism stays).
- The defensive read with three branches (S-5: each branch is earned and the dual log-message distinction matters operationally).
- `_serialize`/`_deserialize` as a symmetric pair using stdlib only (S-4: Pydantic adds 8MB transitive surface for two helpers; structural validation IS the value, not boilerplate).
- The `(info, used_cache_fallback)` tuple return shape (S-2: 2-tuple with one consumer is appropriately minimal; introducing a `FetchResult` dataclass is premature).
- Module docstring length (every paragraph is WHY, no narration).
- `_validate_playlist_id` as a named helper (≥3 caller threshold met).
- `_log.warning` at both drop sites (operational visibility on cache corruption is non-trivial value).
- The hardcoded 24h `_TTL_SECONDS` constant (per `M1-simplify.md` §3, locked).
- The `playlists/` subdirectory layout (per M1 §4, locked).

---

## Total impact

- **LOC saved (source):** ~25–30 (S-1: 15, S-3: 3, S-6: 6, S-11: 3, S-12: 1).
- **LOC saved (tests):** ~50–60 (S-1: 25, S-7: 10 standalone, S-8: 9, S-9: 12, S-10: 10; some overlap between S-1 and S-7 — count once).
- **Realistic merged total: ~70–90 LOC** out of +564, ~13%.
- **Net file ratio:** PR drops from 209/355 (1:1.7) to ~180/300 (1:1.7) — the test-to-source ratio stays the same, which is correct: the testing isn't the bloat; the production-side `is_stale` second-read is.

The brief's "209 LOC vs ~150 estimate" diagnosis was right and S-1 is most of the answer — `is_stale`'s second-read drives the whole `_PLAYLISTS_DIR` constant + `_validate_playlist_id` re-call + duplicated path computation through a private function whose only job is to recover one int the caller already paid for. Fixing that pulls ~15 LOC of structure out of the production file and ~25 LOC of test scaffolding (the `_write_with_fetched_at` shadow serializer, the two `is_stale_missing_*` tests). The other findings are polish on top.

The brief's "test scaffolding 355 LOC for 209 LOC of source" diagnosis was *partly* right — the cache subsystem genuinely needs heavy testing (round-trip preservation, validator robustness, fallback contract, boundary semantics), and a 1.7:1 test:source ratio is in the right ballpark for a security-sensitive serialization layer. The bloat is mostly in three places (banners, redundant `is_stale_missing_*` tests, write-side validation duplicating read-side), all S-7/S-8/S-9.

---

## Report

(a) File saved at `/home/user/Projects/ryzic/docs/plans/PR5-simplify.md`.

(b) Top 3:
   1. S-1 — `read()` returns `(info, fetched_at)`; `is_stale(fetched_at)` becomes pure (~40 LOC including tests).
   2. S-7 + S-8 + S-10 — parametrize three test trios, drop ASCII banners (~25–30 LOC).
   3. S-9 — test `_validate_playlist_id` directly, not twice through read/write (~12 LOC).

(c) Total LOC the PR could lose: **~70–90 LOC** out of +564, ~13%. Most of it is one architectural shift (S-1) plus standard test parameterization. No structural rewrites; surface contract unchanged for `fetch_with_fallback`'s caller (S-1 only changes `read()`'s return shape, which has one consumer inside this file).

**Honest verdict:** PR5 is *mostly* tight, with one real diagnosable smell (`is_stale`'s second disk read driving 30+ LOC of architecture and tests). The brief's intuition that the kwarg "requires a disk read" was the right thread to pull. The atomic write, the dual-warning defensive read, the `_serialize`/`_deserialize` pair, and the tuple return shape are all earned. No subsystem-level cuts; this is targeted polish + one module-shape refactor.
