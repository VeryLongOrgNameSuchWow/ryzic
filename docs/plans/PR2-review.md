# PR #2 Review — `feat(ytdlp): wrapper + URL validator + tests`

**Branch:** `feat/ytdlp-and-url-validator` → `main`
**Scope per plan:** `docs/plans/M1.md` §6 and §12 PR3a (yt-dlp wrapper, URL validator, tests; no Discord deps).
**Diff:** +774 LOC across 4 files (`url_validator.py` 30, `ytdlp.py` 278, `test_url_validator.py` 80, `test_ytdlp.py` 386).
**Local verification:** `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run pytest -q` — all green (77 tests pass). Coverage on the new modules: `url_validator.py` 100%, `ytdlp.py` 99% (sole missing line is `_sync_extract`'s sanitize-info return; covered indirectly via the `YoutubeDL` patch test).

---

## Findings

### MEDIUM-1 — Friendly error mapping returns category codes, not the user-facing strings the plan specifies

- **Severity:** MEDIUM
- **Where:** `src/ryzic/ytdlp.py:50-54, 117-128`
- **Why it matters:** `_FRIENDLY_ERROR_PATTERNS` maps known yt-dlp fragments to bare category strings (`"age-restricted"`, `"private video"`, `"region-blocked or unavailable"`) and `_raise_from_download_error` raises `FetchFailed(friendly or detail)`. The plan §3 specifies the user-facing rendering: `"That video is age-restricted and can't be played."`, `"That video is private."`, `"That video is not available in this region."`. Plan §6 says the wrapper raises `FetchFailed(str)` "with first line of `DownloadError`" and the `/play` command does the friendly rendering ("the `/play` command remaps known patterns to friendlier wording" — verbatim from this PR's own module docstring at line 18-19). The PR splits the difference: it does an intermediate categorization that matches neither contract, forcing `/play` to know the category-code vocabulary and re-translate it to a sentence. Two layers of mapping where the plan calls for one.
- **Fix:** Pick one of (a) raise `FetchFailed(detail)` (the unmodified first line) and let `/play` substring-match `"Sign in to confirm your age"` etc. itself — that's what §6 actually specifies, and it keeps the `/play` mapping table self-contained at the call site. Or (b) raise `FetchFailed("That video is age-restricted and can't be played.")` directly here and have `/play` display verbatim. Option (a) is closer to the plan and keeps `ytdlp.py` ignorant of UX strings; option (b) is fine if you want one source of truth. The current shape is the worst of both — the test at `test_ytdlp.py:172-185` even encodes the category vocabulary as the contract. Pick a layer.

### MEDIUM-2 — `logger=_log` bypasses `no_warnings=True` on yt-dlp warnings

- **Severity:** MEDIUM
- **Where:** `src/ryzic/ytdlp.py:104`
- **Why it matters:** Plan §6 lists `"logger": _quiet_logger` — placeholder name implies a logger that drops/quiets messages. The implementation passes the module logger `_log = logging.getLogger(__name__)`. yt-dlp's `report_warning` (verified in `yt_dlp/YoutubeDL.py`) checks `params.get('logger')` *before* `params.get('no_warnings')` — so when a logger is set, **warnings unconditionally hit `logger.warning(message)` regardless of `no_warnings=True`**. That means every yt-dlp WARNING (the "this site is broken" notice, format-fallback warnings, throttling notices, etc.) shows up in the bot's WARNING-level logs, even though the opts dict explicitly says `no_warnings=True`. The intent of the plan was the opposite — silence yt-dlp output and only surface errors. Same path applies to `to_screen` (becomes `logger.debug` — bypasses `quiet=True`), but that's at DEBUG so cosmetic.
- **Fix:** Either don't pass a `logger` at all (then `quiet=True, no_warnings=True` work as advertised), or define a minimal "quiet" logger that drops `WARNING` and below and only forwards `ERROR` (matches the plan's `_quiet_logger` placeholder name). One-liner:
  ```python
  class _QuietLogger:
      def debug(self, msg): pass
      def info(self, msg): pass
      def warning(self, msg): pass
      def error(self, msg): _log.error("yt-dlp: %s", msg)
  ```

### LOW-1 — `download()` builds opts that include `paths.home` which is silently ignored

- **Severity:** LOW
- **Where:** `src/ryzic/ytdlp.py:93, 273`
- **Why it matters:** `_base_opts` sets `paths={"home": str(cache_root / "tmp")}`. `download()` then sets `outtmpl = str(resolved_dest)` — an absolute path. yt-dlp's `prepare_filename` (verified in source) emits a warning and ignores `paths` whenever `outtmpl` is absolute — that warning is currently swallowed by `quiet=True` (or routed via the logger per MEDIUM-2). Net effect: `paths.home` is dead config for `download()`. It's also dead for `resolve_track`/`resolve_playlist` since `extract_info(..., download=False)` doesn't write files. The line earns its keep nowhere in this PR — partial-file orchestration is owned by `audio_cache.py` in PR3b per plan §4. Not a correctness bug; just config that doesn't do what its presence implies.
- **Fix:** Two reasonable options. (a) Drop `paths` from `_base_opts` and let the cache layer (PR3b) add it when relevant — keeps each module's opts honest. (b) Leave it as a defensive default for any code path that *does* use a relative `outtmpl` (none currently). I'd lean (a); the dead key adds a future maintenance reading-cost.

### LOW-2 — `_extract` only catches `DownloadError`; sibling `YoutubeDLError` subclasses fall to "internal error"

- **Severity:** LOW
- **Where:** `src/ryzic/ytdlp.py:208-215`
- **Why it matters:** `yt_dlp.utils` exposes ~18 exception classes inheriting from `YoutubeDLError` directly (not `DownloadError`): `ExtractorError`, `GeoRestrictedError`, `UnavailableVideoError`, `RejectedVideoReached`, `UnsupportedError`, `UserNotLive`, `ContentTooShortError`, etc. In normal `extract_info` flow, yt-dlp catches `ExtractorError` internally and re-raises as `DownloadError` via `report_error` — confirmed in `yt_dlp/YoutubeDL.py`. So in practice every user-facing failure becomes a `DownloadError` and the current handler works. But the assumption is implicit and brittle to yt-dlp version drift; a release that bypasses the wrap (or a future code path that calls `process_ie_result` more directly) would surface as `"internal error: GeoRestrictedError"` rather than the friendly mapping. The wider catch costs one line.
- **Fix:** Catch `yt_dlp.utils.YoutubeDLError` (the parent of `DownloadError`) instead. The first-line / friendly-mapping logic still works since both expose `str(exc)`. Defensive widening, no behavior change for current yt-dlp versions.

### LOW-3 — `_reject_livestream_filter` and `_check_not_livestream` have nearly-identical bodies

- **Severity:** LOW
- **Where:** `src/ryzic/ytdlp.py:135-144`
- **Why it matters:** `_check_not_livestream` raises `FetchFailed("livestream")` if `_is_livestream(info)` is true; `_reject_livestream_filter` returns the string `"livestream"` (yt-dlp's match-filter contract: returning a non-None reason aborts). Two functions, both wrapping the same one-line predicate. The duplication is explicitly motivated as defense-in-depth (lines 276-278 say so). Fine. But `_reject_livestream_filter`'s `incomplete: bool = False` parameter is unused and exists only because yt-dlp may pass it as a kwarg (per `_match_entry` in yt-dlp source: `match_filter(info_dict, incomplete=incomplete)` with a `TypeError` fallback to `match_filter(info_dict)`). The kwarg exists, but the comment doesn't say so — a reader has to grep yt-dlp to figure out why an unused parameter is there. Trivial — call out the WHY.
- **Fix:** Either drop `incomplete=False` (yt-dlp's `TypeError` fallback handles old-style filters too — verified in source) and let the function take `info` only, or add a one-line comment explaining yt-dlp's contract. Both cost ~zero; the former is one fewer parameter.

### LOW-4 — `assert isinstance(ALLOWED_HOSTS, frozenset)` test is a tautology

- **Severity:** LOW
- **Where:** `tests/test_url_validator.py:79-80`
- **Why it matters:** The test asserts that the literal `frozenset(...)` constructor returned a `frozenset`. That's not testable behavior — it's a Python language guarantee. The intent is "this set must be immutable so middleware can't poison the allowlist at runtime", but the test as written would only fail if someone *changed the source* to `set(...)` — at which point ruff/ty/grep would also flag it. Test scaffolding richer than the surface (PR1-simplify rule #9, applied to tests).
- **Fix:** Drop the test, or replace with a concrete behavioral assertion (`with pytest.raises(AttributeError): ALLOWED_HOSTS.add("evil.com")`). The latter at least exercises the immutability surface that the comment implies.

### LOW-5 — Module docstring narrates the public surface in prose; redundant with the dataclass/function signatures

- **Severity:** LOW
- **Where:** `src/ryzic/ytdlp.py:1-21`
- **Why it matters:** The 21-line docstring opens with "Module functions only — no class. ..." and then enumerates `resolve_track`, `resolve_playlist`, `download`, `validate_video_id` with one-line summaries — duplicating what a reader gets by glancing at the four `def` lines below. The first paragraph (async wrapper, `to_thread`, embedded API, no subprocess/shell) is the WHY worth keeping; the bulleted public surface is WHAT-narration that the type signatures already convey. Project standards: WHY only when non-obvious.
- **Fix:** Trim to the first paragraph (lines 1-7) plus the closing two sentences about errors/logging (lines 17-21). Drop lines 8-16. ~10 LOC saved without information loss.

### LOW-6 — `# type: ignore[no-any-return]` on `sanitize_info` deserves a one-line WHY

- **Severity:** LOW
- **Where:** `src/ryzic/ytdlp.py:194`
- **Why it matters:** `# type: ignore` directives without a comment are project smell — future maintainers can't tell whether this is "the upstream stub is wrong" or "I gave up". `yt_dlp` doesn't ship a `py.typed` marker, so all return types are `Any`; the ignore is silencing ty's "function declared to return `dict[str, Any]` but the call expression's type is unknown". One short trailing comment saves the next reader a 3-minute investigation.
- **Fix:** `return ydl.sanitize_info(info)  # type: ignore[no-any-return]  # yt-dlp ships no type stubs` — or similar.

### LOW-7 — `paths={"home": str(cache_root / "tmp")}` does not pre-create the `tmp/` directory

- **Severity:** LOW (out-of-scope-leaning)
- **Where:** `src/ryzic/ytdlp.py:93`
- **Why it matters:** If `cache_root` exists but `cache_root / "tmp"` does not, `download()` will fail at write time. yt-dlp's `paths.home` is a configuration hint, not a directory-creator. In practice PR3b's audio_cache will create `tmp/` at startup, so this isn't a defect *given the orchestration plan*. Flagging only because if anyone calls `download()` standalone (as the unit tests do) without a pre-existing `tmp/` dir, they'd get a confusing FS error. The existing tests dodge it because they patch `_sync_extract` and never let yt-dlp touch disk.
- **Fix:** None required for this PR — formalize the precondition in the docstring of `download()` ("`cache_root / 'tmp'` must exist") so PR3b reviewers don't miss it. Or `cache_root.joinpath("tmp").mkdir(parents=True, exist_ok=True)` in `download()` — three lines, idempotent.

### INFO — Things that are genuinely solid (worth saying)

- **`url_validator.py`** is exemplary: 30 LOC, one function, one `frozenset`, and a module docstring whose entire payload is the WHY (regex-based validators happily match `youtube.com.evil.com`). Test coverage hits every allowlisted host individually, the precise HIGH-1 regression, HTTP downgrade, three exotic schemes, two other-platform negatives, malformed/empty inputs, the IPv6 `ValueError` swallow, and the userinfo-smuggle case (`https://youtube.com@evil.com/...`). Case-insensitivity test exists and passes (urlparse lowercases hostnames). The implementation correctly compares `parsed.hostname` (post-userinfo) rather than `parsed.netloc` (which would include `user@host`).
- **`ytdlp.py` security posture** is tight: `cookiefile=None` with the verbatim comment ("DO NOT enable... requires its own security review before flipping"); `geo_bypass=False`; `max_filesize=500_000_000`; `playlist_items="1-1000"`; `concurrent_fragment_downloads=1`; `restrictfilenames=True`; embedded API only (no subprocess, no shell). All seven security-critical assertions covered by `test_base_opts_security_critical_settings`. Defense-in-depth livestream check (post-extract `_check_not_livestream` PLUS `match_filter=_reject_livestream_filter`) is the right pattern — the filter aborts before bytes hit disk, the post-check covers the resolve_track path that has no filter.
- **Async correctness**: every yt-dlp call goes through `asyncio.to_thread(_sync_extract, ...)` in `_extract`. There are no sync-blocking yt-dlp calls in the public surface. `_extract` is the single chokepoint, which means future call sites can't accidentally bypass the threading.
- **Path safety**: `validate_video_id` enforces `^[A-Za-z0-9_-]{6,20}$` BEFORE any path construction (called from `_track_from_info`, `_entry_from_flat`). `download()` independently enforces `dest.resolve().relative_to(cache_root.resolve())` and raises `InvalidVideoID` on escape — sandbox check happens before yt-dlp is invoked (`m.assert_not_called()` test confirms). Two layers of validation; appropriate for the file-write boundary.
- **Error normalization is a strict layered chain**: `_extract` re-raises `FetchFailed` (so internal raises propagate cleanly), translates `DownloadError` via `_raise_from_download_error`, and wraps everything else with `_log.exception` + `FetchFailed("internal error: ClassName")`. The final-fallback log uses `_log.exception` so the full trace lands in the logs while only the class name reaches the user — exactly the contract §6 specifies.
- **`extract_flat` correctness**: `resolve_playlist` overrides both `extract_flat=True` and `noplaylist=False` on a per-call basis (lines 238-239); the base opts default to `noplaylist=True, extract_flat=False` for `resolve_track` and `download`. The opts dict isn't shared mutable state (each `_base_opts` call returns a fresh dict — covered by `test_base_opts_does_not_leak_global_state`).
- **`_entry_from_flat` skip-and-continue policy**: invalid IDs / missing IDs / non-dict entries are dropped silently and the playlist still resolves (covered by `test_resolve_playlist_skips_invalid_entries` with four bad-entry shapes). This matches plan §3 ("Playlist partial-failure: ... `{X} tracks could not be loaded` footer line") — partial failure is a data point for the embed, not an abort condition.
- **Test depth**: 77 tests, 99-100% coverage on new modules, all yt-dlp interactions mocked (no network). Every edge case in the M1 spec has a corresponding test (HIGH-1 regression, HTTP downgrade, livestream active+upcoming+was_live, three friendly mappings, unknown-error first-line passthrough, internal-error wrapping with caplog assertion, sandbox-escape rejection without invoking yt-dlp, all security-critical opts).
- **Frozen dataclasses** for `TrackInfo` and `PlaylistInfo` — value semantics, no accidental mutation, hashable. Good fit for cache-key contexts in PR3b/PR4.
- **No premature abstractions**: no `YtdlpClient` class, no plugin/strategy pattern for error mapping, no `Protocol` for the loggers. Module functions throughout, per `M1-simplify.md` §10. The two error-mapping helpers (`_first_line`, `_map_friendly`) are small, single-purpose, and called once each — borderline-inlineable but break out usefully for the pure-function unit tests at `test_first_line_extracts_first_non_empty`.
- **Conventional commit**: single commit `feat(ytdlp): wrapper + URL validator + tests` with a precise body; co-author tag present. Will squash-merge cleanly.
- **No Discord coupling**: confirmed via `grep -r hikari\\|lightbulb src/ryzic/url_validator.py src/ryzic/ytdlp.py` — zero hits. Per plan §12 PR3a constraint.

---

## Overall verdict

**minor revisions** — none of the findings block downstream PRs (PR3b can build on `TrackInfo`/`PlaylistInfo` as-is and the path-safety contract is sound), but MEDIUM-1 (friendly-error layer mismatch with plan) and MEDIUM-2 (`logger` bypass of `no_warnings`) are both worth fixing in this PR rather than deferring. MEDIUM-1 in particular will create awkward duplication when `/play` gets implemented in PR6a — better to pick the layer now while the surface is fresh. The LOW items are 5–10 LOC of polish each, can be batched into a single follow-up commit on this branch or rolled forward.

**One-line summary:** Tight, security-conscious yt-dlp wrapper with exemplary URL validator and 99% test coverage; main issue is the friendly-error layer doing one-and-a-half mappings (returns category codes, not first-lines or finished sentences) — pick one or the other before `/play` lands.
