# PR #5 — Security Review

**PR**: `feat(cache): playlist metadata cache (live-first with TTL fallback)`
**Branch**: `feat/playlist-cache` → `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commit reviewed**: `1980b4a`
**Files in scope** (PR diff only):
- `src/ryzic/playlist_cache.py` (new, 209 lines)
- `tests/test_playlist_cache.py` (new, 355 lines)

No dependency or build-system changes (`pyproject.toml`/`uv.lock` unchanged from PR2).

This review covers the nine focus areas from the brief: `playlist_id` regex robustness, path safety, JSON read trust boundary, atomic-write file mode, `fetch_with_fallback` error leakage, log content, `_deserialize` round-trip type safety, test isolation, and dependency surface.

The 42 tests in `tests/test_playlist_cache.py` all pass under `uv run pytest -q`.

---

## Findings

### 1. `_PLAYLIST_ID_RE` allows trailing newline → cache poisoning + log injection

**Severity**: HIGH
**What**: The regex `^[A-Za-z0-9_-]{10,50}$` is matched against the playlist id with no `re.MULTILINE` flag, but Python's default `$` *also* matches the position immediately before a final `\n`. So `"PLabcdefghi\n"` (12 chars including a trailing newline) passes `_validate_playlist_id`, and `_path_for` then composes `cache_root/playlists/PLabcdefghi\n.json` — a different filename from the canonical `PLabcdefghi.json`.

End-to-end exploitability via a Discord-supplied URL (verified with a POC against the unmodified branch):

1. Attacker sends `/play https://www.youtube.com/playlist?list=PLabcdefghi%0a` (URL-encoded `%0a` = `\n`).
2. `_extract_playlist_id` calls `parse_qs`, which percent-decodes `%0a` to a literal `\n`, yielding the candidate `"PLabcdefghi\n"`.
3. `_PLAYLIST_ID_RE.match(candidate)` returns a match (the regex pitfall above), so the candidate is returned.
4. If yt-dlp's `resolve_playlist` ever fails for this URL, `read("PLabcdefghi\n", cache_root)` is called → `_path_for` validates the regex (passes), then composes `cache_root/playlists/PLabcdefghi\n.json`.
5. An attacker who has previously seeded that cache file (via the same trick on a successful live fetch — yt-dlp would presumably return `playlist_id="PLabcdefghi"` (without newline) so the cache write goes to the canonical filename, but the read on the newline variant would return `None`. **Not a write-side cache poison via yt-dlp.**)

The actual attack surface, then, is narrower than first appears, but still real:

   - **Cache namespace pollution / collision**: `read("PLabcdefghi\n", ...)` and `read("PLabcdefghi", ...)` resolve to **different files** despite being the same logical playlist. After PR6 wires `/play` up, two URLs that differ only by `%0a` will be served from different cache entries with no operator visibility into why.
   - **Log injection** (LOW on its own, but enabled by this same root cause): `_log.warning("yt-dlp failed for playlist %s; serving cached metadata: %s", playlist_id, exc)` interpolates the unsanitized `playlist_id` (which now contains `\n`) directly into the log line. POC log output (line break is real, copied verbatim from a run):
     ```
     yt-dlp failed for playlist PLabcdefghi
     ; serving cached metadata: yt-dlp down
     ```
     A determined attacker could include `\n` plus a forged second-line prefix to make log scrapers see fabricated ERROR entries from other modules.
   - **Filesystem garbage**: writes via `write()` directly (e.g. when yt-dlp echoes back a malformed id, however unlikely) would create on-disk filenames containing control characters. Annoying for ops, harmless to security.

The same bug also accepts `\r` only at end-of-string? No — `\r` is not in the special-case list; only `\n` is. Confirmed by `re.match(r"^abc$", "abc\r")` returning `None`.

**Where**: `src/ryzic/playlist_cache.py:32` (`_PLAYLIST_ID_RE`). Affects every callsite: `read`, `write`, `is_stale`, `_extract_playlist_id`, plus the test invariant in `test_validate_rejects_invalid_on_read` / `test_validate_rejects_invalid_on_write` (which lacks a `\n`-suffix case and so doesn't catch this).

**Why it matters**: This regex is the entire trust boundary for everything path- and log-related in this module. It's also the exact concern called out in the brief ("regex enforced BEFORE any path join? Edge cases: empty, leading dot, `..`, traversal attempts (`%2e%2e`, unicode normalization)?"). The path-traversal cases the dev tested for (`../`, `%2e%2e`, `\x00`) all fail the regex correctly because the offending chars are outside the charset; `\n` slips through because of Python's quirky default-mode `$`. There's no `path.relative_to(cache_root)` second-wall check anywhere in the module to catch a regex regression.

**Fix**:
- **Preferred**: change the validator to `_PLAYLIST_ID_RE.fullmatch(playlist_id)` (one-word change in `_validate_playlist_id` AND in `_extract_playlist_id`'s ternary). `fullmatch` requires the regex to match the entire string and is immune to the trailing-`\n` quirk.
- **Or**: change the regex to `r"\A[A-Za-z0-9_-]{10,50}\Z"` (raw `\A` and `\Z` anchors are absolute, unlike `^` and `$`).
- **Defense-in-depth**: add `path.resolve().relative_to(cache_root.resolve())` inside `_path_for` (or a standalone helper) so a future regex regression cannot escape the cache directory.
- **Test coverage**: add `"PLabcdefghi\n"`, `"PLabcdefghi\r\n"`, `"\nPLabcdefghi"` (already rejected — newline at front is blocked by `^`) to the `test_validate_rejects_invalid_on_*` parametrize lists. Also add a parametrized URL test for `?list=PLabcdefghi%0a` going through `fetch_with_fallback`.

---

### 2. JSON cache file has no size cap; `path.read_text()` reads entire file into memory

**Severity**: LOW (different threat model)
**What**: `_read_sync` calls `path.read_text(encoding="utf-8")` followed by `json.loads(text)`. Neither has a size cap. A 1 GB cache file would consume 1 GB of RSS while being parsed.
**Where**: `src/ryzic/playlist_cache.py:88-92` (`_read_sync`).
**Why it matters**: Not exploitable from a Discord URL — the only writer of cache files is the bot itself, and the writer is bounded by yt-dlp's `playlist_items="1-1000"` cap (per PR2). A 1000-track flat playlist serializes to roughly 200 KB. The risk is a co-resident or post-compromise attacker filling cache files with junk to degrade the bot. PR2's review item #8 already documents that the cache directory's deploy-time permissions (0o700, bot-owned) are the mitigation here — extending that to "the bot trusts the contents of files it can write" is consistent.
**Fix** (optional defensive): cap `_read_sync` at e.g. 5 MB by `path.stat().st_size > 5 * 1024 * 1024` early-return + log warning. The dev cost is small; the operational benefit is marginal but real for forensic clarity. I'd defer this to the M2 audio-cache PR which faces the same issue.

---

### 3. `_deserialize` `int()` cast on `duration_ms` is bounded, but other casts have no overflow protection

**Severity**: LOW
**What**: `_deserialize` constructs each `TrackInfo` with `int(e["duration_ms"])`. This call delegates to Python's built-in `int()`, which since Python 3.11 enforces a 4300-digit limit on string→int conversion (raises `ValueError`). The brief asks whether `duration_ms` is validated as an int rather than something that explodes later — it is, and `_deserialize` correctly catches `ValueError` and treats the file as malformed (`read()` returns `None`).

I verified the limit: `int("9" * 1_000_000)` raises `ValueError: Exceeds the limit (4300 digits)...` — and `_deserialize`'s try/except in `read()` catches it. Round-trip with 100 000 entries deserializes in ~50 ms, well below any DoS threshold.

The `str(...)` casts on `video_id`, `url`, `title`, `uploader` are unbounded but stringifying any JSON value is bounded by file size (#2). No structural mismatch can cause a crash later in the pipeline because the dataclass is frozen and downstream consumers (per the brief, future PRs) will treat its fields as opaque strings.

**Where**: `src/ryzic/playlist_cache.py:62-86`.
**Verdict**: clean for the spec'd threat model. **No fix required.**

---

### 4. Atomic write uses default umask; tempfile name is predictable

**Severity**: LOW
**What**: `_write_sync` calls `path.parent.mkdir(parents=True, exist_ok=True)` then `tmp.write_text(...)` then `tmp.replace(path)`. Both `mkdir` and `write_text` honor the process umask. With a typical Linux umask of `0o022`:

- `playlists/` directory: `0o755` (world-traversable)
- `<id>.json.tmp` and `<id>.json`: `0o644` (world-readable)

Empirically verified.

For a self-hosted bot in a container with `cache_root` owned 0o700 by the bot UID, the inherited 0o755/0o644 modes are masked by the parent's mode bits — nothing leaks. But if `cache_root` were ever 0o755 (e.g. a misconfigured bind mount), the per-file 0o644 means anyone in the host filesystem can read playlist titles and uploader names. Low signal value, but worth noting.

The tempfile name `<id>.json.tmp` is **fully predictable**. A co-resident attacker with write access to `cache_root/playlists/` could `mkfifo cache_root/playlists/PLabcdefghi.json.tmp` or symlink it to `/path/the/bot/can/write/but/shouldnt`. The bot's `tmp.write_text(...)` would either block (FIFO) or follow the symlink and clobber the target. This is the same threat model as PR2 §8 — assumes a co-resident attacker with cache write permission, which the deployment doc must rule out via 0o700 ownership.

**Where**: `src/ryzic/playlist_cache.py:95-104`.
**Why it matters**: Defense-in-depth; not exploitable from a Discord URL.
**Fix** (optional): use `tempfile.NamedTemporaryFile(dir=path.parent, delete=False, prefix=f"{path.name}.", suffix=".tmp")` to randomize the tempfile suffix; close + `os.replace` to the final path. This eliminates the predictable-name TOCTOU. Per-file mode (0o600 instead of umask-default) can be set with `os.fchmod(f.fileno(), 0o600)` before close. Worth bundling into a generic `atomic_write` helper if/when the audio-cache PR lands the same primitive.

---

### 5. `fetch_with_fallback` re-raises `FetchFailed` from the wrapper — already scrubbed by PR2

**Severity**: (informational — no finding)
**What**: When `resolve_playlist` raises `FetchFailed`, `fetch_with_fallback` may re-raise it unchanged (no cache hit) or log+swallow (cache hit). Both paths preserve the original exception object via `raise` (no rebinding). The exception's `args[0]` was constructed by the PR2 wrapper, which already runs every yt-dlp error through `_scrub` (strips backticks, masks absolute paths, caps at 200 chars) before wrapping in `FetchFailed`.

I traced the flow: `_extract` → `except YoutubeDLError as exc` → `scrubbed = _scrub(detail)` → `friendly = ... f"yt-dlp said: \`{scrubbed}\`"` → `raise FetchFailed(friendly) from exc`. The internal-exception path (`except Exception`) wraps as `FetchFailed(f"internal error: {exc.__class__.__name__}")` — class name only, no message text.

So the cached-fallback log line `_log.warning("...serving cached metadata: %s", playlist_id, exc)` cannot leak unsanitized yt-dlp internals. The downstream `/play` embed (future PR) will need to keep treating `FetchFailed.args[0]` as already-clean (PR2's posture) or re-scrub at the embed boundary — but that's not a PR5 concern.

**Where**: `src/ryzic/playlist_cache.py:193-207`; relies on `src/ryzic/ytdlp.py:200-218`.
**Verdict**: clean.

---

### 6. Log content includes raw `playlist_id` (subject to finding #1)

**Severity**: LOW (rolled into HIGH-1)
**What**: Three `_log.warning` calls embed `playlist_id` or `path` (which contains `playlist_id`) directly into the log line. With the regex hardened per finding #1, all of these will only ever see `[A-Za-z0-9_-]{10,50}` — perfectly safe. Without the fix, the trailing-`\n` log-injection demonstrated in #1 applies. No additional fix needed beyond #1.
**Where**: `src/ryzic/playlist_cache.py:120, 127, 202`.

---

### 7. Symlink at `cache_root/playlists/` is followed (out-of-scope ops concern)

**Severity**: LOW (different threat model, inherited from PR2 §8)
**What**: `_write_sync` calls `path.parent.mkdir(parents=True, exist_ok=True)` — if `playlists/` already exists as a symlink to e.g. `/etc/`, the bot will `mkdir` the (already-existing) target and write JSON files into it. `_read_sync` and `is_stale` follow the symlink chain too.
**Where**: `src/ryzic/playlist_cache.py:97`.
**Why it matters**: Requires a co-resident attacker who can pre-create the symlink before the bot's first `write()`. PR2's §8 covers the same class of issue for the audio cache; the mitigation is the same — `cache_root` permissions 0o700 owned by the bot's UID.
**Fix** (defer): when a generic atomic-write helper lands, also assert `path.parent.is_symlink()` is False, or use `O_NOFOLLOW` opens. Not blocking.

---

### 8. Tests are network-isolated and tmp_path-only

**Severity**: (informational — no finding)
**What**: `tests/test_playlist_cache.py` imports only stdlib + `pytest` + the project's own modules. No `requests`, `httpx`, `urllib.request`, or `socket` imports. The yt-dlp call is mocked via `unittest.mock.patch.object(playlist_cache, "resolve_playlist", ...)` in every fallback test. All disk usage flows through the `tmp_path` fixture (pytest-managed, auto-cleaned). The `_write_with_fetched_at` helper writes only under `tmp_path`. The `test_traversal_id_does_not_create_file_outside_cache` test goes one step further and asserts no leakage into `tmp_path.parent`.
**Verdict**: clean.

---

### 9. Dependency surface — no changes from PR2

**Severity**: (informational — no finding)
**What**: `git diff main..feat/playlist-cache -- pyproject.toml uv.lock` is empty. The new module imports only from stdlib (`asyncio`, `json`, `logging`, `re`, `time`, `dataclasses`, `pathlib`, `typing`, `urllib.parse`) and the project's own `errors` and `ytdlp` modules.
**Verdict**: clean.

---

## Summary

| Severity | Count |
| --- | ---: |
| HIGH (merge-blocking) | **1** |
| MEDIUM (fix soon) | 0 |
| LOW (nit / defense-in-depth) | 5 |

The HIGH is **#1** — the `^...$` vs. `\n` regex pitfall, exploitable end-to-end from a Discord-supplied URL via `%0a`-encoded list parameter, leading to cache namespace pollution and log injection. The fix is a one-character change (`match` → `fullmatch`, used in two spots) plus tests. Low cost, high payoff, and matches the spec's intent of "validation BEFORE any path join."

The LOWs are: cache-file size cap (#2), atomic-write umask + predictable tempfile name (#4), log injection (#6 — duplicate of #1), and symlink follow on `playlists/` (#7). All four are defense-in-depth; none are exploitable from a Discord URL given the current call surface; #4 and #7 echo PR2's review and should be bundled into a future shared `atomic_write` helper (likely M2's audio cache).

The brief's other primary concerns — `_deserialize` int-cast safety (#3), error-message leakage from yt-dlp via `fetch_with_fallback` (#5), test isolation (#8), and dependency drift (#9) — are all clean.

## Verdict

**fixes recommended**

Not strictly merge-blocking on its own (the cache poisoning has no live consumer until PR6 wires `/play`), but the fix for #1 is so cheap and so structurally important that it should land in this PR rather than as a follow-up. Treat #1 as a hard "fix before merge" and the LOWs as polish — most are best addressed when the matching primitives are needed for the audio cache.
