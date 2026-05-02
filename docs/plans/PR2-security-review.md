# PR #2 — Security Review

**PR**: `feat(ytdlp): wrapper + URL validator + tests`
**Branch**: `feat/ytdlp-and-url-validator` → `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commit reviewed**: `092c6a1`
**Files in scope** (PR diff only):
- `src/ryzic/url_validator.py` (new, 30 lines)
- `src/ryzic/ytdlp.py` (new, 278 lines)
- `tests/test_url_validator.py` (new, 80 lines)
- `tests/test_ytdlp.py` (new, 386 lines)

No dependency or build-system changes (`pyproject.toml`/`uv.lock` unchanged). `yt-dlp>=2026.3.17` was already declared in PR #1.

This review covers the eight security focus areas from the brief: URL-validator correctness, yt-dlp invocation safety, path traversal, livestream DoS, error-message leakage, resource exhaustion, test isolation, and dependency surface.

---

## Findings

### 1. URL validator — correctness

**Severity**: (informational — no finding)
**What**: `is_supported_url` uses `urlparse(url).hostname` against a hardcoded `frozenset` allowlist and requires `scheme == "https"`. I exercised every attack vector listed in the brief plus several extras (CRLF/TAB stripping per RFC 3986 normalisation, Cyrillic homoglyphs, NUL bytes, embedded credentials with port, raw IPv6 brackets, path-only URLs, etc.). Every hostile input is rejected; every allowlisted host is accepted (case-insensitively, courtesy of `urlparse` lowercasing).

Sampled cases I ran outside the test suite:

| Input | Result | Outcome |
| --- | --- | --- |
| `https://youtube.com.evil.com/x` | hostname=`youtube.com.evil.com` | rejected |
| `https://youtube.com@evil.com/x` | hostname=`evil.com` | rejected (userinfo split) |
| `https://www.youtube.com\@evil.com/x` | hostname=`evil.com` | rejected |
| `https://xn--youtube-com.evil.com/x` | hostname=`xn--youtube-com.evil.com` | rejected |
| `https://yоutube.com/x` (Cyrillic 'о') | hostname=`yоutube.com` (no IDNA fold) | rejected |
| `https://youtube.com\x00.evil.com/x` | hostname=`youtube.com\x00.evil.com` | rejected |
| `http://youtube.com/x` | scheme=`http` | rejected (downgrade) |
| `https://[::1` | `urlparse` raises `ValueError` | rejected (caught) |
| `https://youtube.com/\r\nHost: evil.com` | CRLF stripped by `urlparse` → hostname=`youtube.com` | accepted (no smuggling vector — CRLF never reaches yt-dlp) |
| `https://youtube.com.` (trailing dot FQDN) | hostname=`youtube.com.` | rejected (UX nit, see #2) |

`ALLOWED_HOSTS` is a `frozenset` (asserted in `test_allowed_hosts_is_immutable`). The `ValueError` swallow on malformed IPv6 is explicit and tested. **Verdict: clean.**

---

### 2. URL validator — trailing-dot FQDN rejected

**Severity**: LOW (UX nit, not a security risk)
**What**: `https://youtube.com./watch?v=...` (trailing-dot rooted FQDN — DNS-equivalent to `youtube.com`) is rejected because the hostname `youtube.com.` is not in `ALLOWED_HOSTS`.
**Where**: `src/ryzic/url_validator.py:13-21`.
**Why it matters**: Some link-paste workflows produce trailing-dot URLs. Users will see "URL not supported" for what is in fact a valid YouTube link. Not exploitable.
**Fix** (optional): strip a single trailing `.` before the lookup, e.g. `host = (parsed.hostname or "").removesuffix(".")`. Or accept the nit — likelihood is low.

---

### 3. yt-dlp plugin auto-loading is enabled by default

**Severity**: MEDIUM (defense-in-depth gap)
**What**: yt-dlp ≥ 2025 auto-loads plugins on every `YoutubeDL(...)` instantiation, scanning user/system config dirs (`~/.config/yt-dlp/plugins/`, `/etc/yt-dlp/plugins/`, `~/.yt-dlp-plugins/`) and **every entry in `sys.path`** for namespace packages named `yt_dlp_plugins`. The default is `plugin_dirs = ['default']`. Our `_base_opts` does not set `plugin_dirs: []` and the codebase does not export `YTDLP_NO_PLUGINS=1`. I verified live: `from yt_dlp.plugins import plugin_dirs; print(plugin_dirs.value)` → `['default']`.
**Where**: `src/ryzic/ytdlp.py:79-105` (`_base_opts`); also missing from any module-level setup.
**Why it matters**: For self-hosters, this means any local package that ships a `yt_dlp_plugins` subpackage (intentionally or by typo-squat in a future `pip install`) gets executed inside the bot process, with full access to the Discord token and the cache filesystem. Exploiting it requires either local write access or a malicious dependency in the env — both are bigger problems on their own — but disabling plugins is a free belt-and-braces hardening that aligns with M1 §6's "embedded API; no subprocess; no shell" posture. The plan does not enumerate this risk; it should.
**Fix**: Add `plugin_dirs: []` to `_base_opts`, or set `os.environ["YTDLP_NO_PLUGINS"] = "1"` once at module import. Add a `test_base_opts_disables_plugins` assertion. Optionally pair with `allowed_extractors=["youtube", "youtube:tab", "youtube:playlist"]` (see #4).

---

### 4. `allowed_extractors` not constrained

**Severity**: LOW (defense-in-depth gap)
**What**: yt-dlp's `allowed_extractors` defaults to `['default']`, which permits all bundled extractors including `Generic` (which probes arbitrary HTML for `<video>` tags / m3u8 / etc.). Our hostname allowlist already pins URLs to YouTube domains, so in normal flow only the YouTube extractor matches — but if the YouTube extractor ever reorders its URL match precedence below `Generic`, or if a YouTube URL with an unusual shape falls through to `Generic`, an attacker-controlled YouTube redirect could be processed by an unintended extractor.
**Where**: `src/ryzic/ytdlp.py:79-105`.
**Why it matters**: Belt-and-braces. The hostname allowlist is the primary defense; this would be a second wall.
**Fix**: Add `"allowed_extractors": ["youtube", "youtube:tab", "youtube:playlist", "youtube:search"]` (verify exact extractor IDs you need at integration time). Add a test that `youtube` is in the list and `Generic` is not.

---

### 5. yt-dlp invocation surface — embedded API only, options frozen, sandboxed

**Severity**: (informational — no finding)
**What**: All yt-dlp work goes through `with YoutubeDL(opts) as ydl: ydl.extract_info(...)` dispatched via `asyncio.to_thread`. No `subprocess`, no shell, no `os.system`, no `Popen`. `_base_opts` returns a fresh dict per call (`test_base_opts_does_not_leak_global_state` confirms). All security-critical options are tested (`test_base_opts_security_critical_settings`):

| Option | Setting | Tested |
| --- | --- | --- |
| `cookiefile` | `None` (with security comment forbidding enablement) | yes |
| `geo_bypass` | `False` | yes |
| `max_filesize` | `500_000_000` (500 MB cap) | yes |
| `playlist_items` | `"1-1000"` (cap) | yes |
| `concurrent_fragment_downloads` | `1` | yes |
| `restrictfilenames` | `True` | yes |
| `paths.home` | `cache_root/tmp` (sandboxed) | yes |
| `format` | Lavaplayer-friendly codec allowlist | yes |
| `noplaylist` / `extract_flat` | `True` / `False` (overridden in `resolve_playlist`) | yes |

Argv injection is N/A by construction (no argv). `cookiesfrombrowser` is not set (defaults to None), but is *not* mentioned in the cookies comment — see #6. yt-dlp does not pickle untrusted input. The comment at lines 99-103 documents that flipping `cookiefile` requires its own security review. **Verdict: clean.**

---

### 6. Cookies comment mentions `cookiefile` only, not `cookiesfrombrowser`

**Severity**: LOW (documentation gap)
**What**: yt-dlp also accepts `cookiesfrombrowser=("chrome", profile, container, domain)` which reads cookies from a local browser profile. Our `_base_opts` does not set it (defaults to None, safe), but the security comment in `src/ryzic/ytdlp.py:99-103` only mentions `cookiefile`. A future contributor reading the comment might enable `cookiesfrombrowser` thinking they were sidestepping the prohibition.
**Where**: `src/ryzic/ytdlp.py:99-103`.
**Fix**: Extend the comment to "cookies (`cookiefile` AND `cookiesfrombrowser`) are deliberately disabled". Optionally add `"cookiesfrombrowser": None` explicitly and assert it in `test_base_opts_security_critical_settings`.

---

### 7. Path traversal — `download` sandbox enforced

**Severity**: (informational — no finding)
**What**: `download(url, dest, *, cache_root)` resolves `dest` via `Path.resolve()` (which canonicalises symlinks and `..`), then calls `resolved_dest.relative_to(resolved_root)`. On `ValueError`, raises `InvalidVideoID` BEFORE any yt-dlp call. The absolute resolved dest is then passed as `outtmpl`. Tests cover the escape-via-absolute-path case and the `..`-traversal case (`test_download_rejects_dest_outside_cache_root`, `test_download_rejects_traversal_dest`), and verify yt-dlp is never invoked on rejection (`m.assert_not_called()`).

`outtmpl` is interpreted by yt-dlp as a Python `%`-format template, but since the resolved dest path is built from a regex-validated `video_id` (`^[A-Za-z0-9_-]{6,20}$`, charset excludes `%`, `(`, `)`, `s`), no template substitution can occur. yt-dlp's `_outtmpl_expandpath` calls `os.path.expandvars` / `expanduser`, but `Path.resolve()` already eliminates `~` and the validated charset excludes `$`.

When `outtmpl` is absolute, yt-dlp explicitly ignores `paths` (verified in `YoutubeDL.prepare_filename` line 1537-1538), so the `paths.home = cache_root/tmp` setting is moot for the final filename — it serves only as the directory yt-dlp would have used had outtmpl been relative. Intermediate (`.partial`/`.part`) files are written next to the absolute outtmpl, which still resolves under `cache_root`. **Verdict: clean.**

`validate_video_id` is exposed for the cache layer's pre-construction use, with a dedicated test set (8 valid + 9 invalid including `../../etc/passwd`, `abc/../etc`, URL-encoded). **Verdict: clean.**

---

### 8. Symlink TOCTOU on cache directory

**Severity**: LOW (different threat model)
**What**: `dest.resolve()` resolves symlinks at check time. If the cache directory has another local user or process with write access, that party could replace `cache_root/audio/dQ` with a symlink to `/etc/` between the `relative_to` check and yt-dlp's actual `open()` call. yt-dlp's `open()` follows symlinks.
**Where**: `src/ryzic/ytdlp.py:265-273`.
**Why it matters**: Requires a co-resident attacker with write access to the cache directory — outside the brief's threat model (untrusted Discord user-supplied URLs). Worth noting in the M2 cache-subsystem PR rather than blocking PR #2.
**Fix** (defer to M2): if you want to guard against this, open the dest with `os.open(dest, O_NOFOLLOW | O_CREAT | O_EXCL, 0o600)` and pass the fd to yt-dlp via a custom downloader — but yt-dlp's downloader accepts paths, not fds, so this is non-trivial. Easier mitigation: ensure cache directory permissions are 0o700 owned by the bot's UID, which is the docker-compose / deploy-doc concern.

---

### 9. Livestream DoS — defended at three layers

**Severity**: (informational — no finding)
**What**: `download()` sets `match_filter=_reject_livestream_filter` BEFORE invoking yt-dlp. yt-dlp calls the filter inside `_match_entry` both with `incomplete=True` (early, before formats are resolved) and `incomplete=False` (later, with full info). The filter checks both `info["is_live"]` and `info["live_status"] in {"is_live", "is_upcoming"}`, returning a non-None abort reason that yt-dlp surfaces as a `RejectedVideoReached` (mapped to `FetchFailed("livestream")` via the friendly-error layer).

After `_extract` returns, `download()` *also* calls `_check_not_livestream(info)` as defense-in-depth (line 277-278, comment explicit). `resolve_track` calls `_check_not_livestream` post-extraction too. `_LIVE_STATUSES = {"is_live", "is_upcoming"}` correctly excludes `was_live` and `post_live` (which are downloadable VODs); `test_resolve_track_accepts_recorded_was_live` confirms.

YouTube extractor populates `live_status` as a top-level key during initial extraction (verified in `yt_dlp/extractor/youtube/_video.py`), so `incomplete=True` already has the data. No window where bytes hit disk before the check. **Verdict: clean.**

---

### 10. Error message leakage — first-line-only, but not stripped of paths/backticks

**Severity**: LOW (downstream consumer responsibility per M1 §6 item 10, but worth flagging here)
**What**: `_first_line(str(exc))` returns only the first non-empty line of yt-dlp's `DownloadError`, which limits multi-line traceback / stack info leakage (tested by `test_resolve_track_unknown_download_error_passes_first_line`). However, the first line itself can contain absolute filesystem paths (yt-dlp errors like `ERROR: unable to write to /home/.../cache/audio/dQ/x.m4a`) and backticks. The wrapper does not scrub these.

`_map_friendly` covers three known patterns (age-restricted / private / unavailable) and emits hardcoded user-presentable strings; for any other yt-dlp error, the raw first line is propagated as `FetchFailed(detail)`. Internal exceptions (anything other than `DownloadError`) are wrapped as `FetchFailed(f"internal error: {exc.__class__.__name__}")` (no message text) with full traceback logged at ERROR — that's well-handled.

The `RuntimeError("kaboom")` path is tested (`test_resolve_track_internal_error_logged_and_wrapped`). Crucially, `caplog` confirms `"kaboom"` appears in logs but the test asserts `match="internal error"` for the user-visible message — exception message is not surfaced. Good.

**Where**: `src/ryzic/ytdlp.py:108-128`.
**Why it matters**: M1 §6 item 10 explicitly says "strip backticks from yt-dlp error strings BEFORE wrapping in inline code". That responsibility lives in the as-yet-unwritten embed builder. If the embed builder consumes `FetchFailed.args[0]` raw, an unsanitised yt-dlp first line could break out of an inline code span (`` `…` ``) or leak the host's `cache_root` path to Discord users. This is a future-PR concern, not a PR #2 bug, **but** the wrapper could provide a sanitised string preemptively (`re.sub(r"[`\\]", "", first_line)` and a length cap) to make consumer code safe-by-default.
**Fix**: consider sanitising in `_first_line`/`_raise_from_download_error`: collapse runs of whitespace, strip backticks, cap at ~200 chars, replace any absolute path that begins with `cache_root` or `/home/` with `<path>`. This is genuinely defensive — the embed-builder author may forget. Or leave as-is and ensure the embed-builder PR's review explicitly covers it.

---

### 11. Resource exhaustion — bounded everywhere

**Severity**: (informational — no finding)
**What**:
- **Per-track size**: `max_filesize=500_000_000` (500 MB).
- **Playlist length**: `playlist_items="1-1000"`.
- **Memory on flat playlist**: `extract_flat=True` returns minimal entries (id/title/url/duration); 1000 such entries is ≪ 1 MB of dict.
- **Subprocess via post-processors**: `_base_opts` declares no `postprocessors` key, so neither `FFmpegExtractAudio`, `FFmpegMerger`, nor `Exec` postprocessor runs by default. Audio-only formats (`bestaudio[ext=...]`) generally don't trigger merging.
- **External downloaders**: not configured; default native HTTP downloader runs in-process.
- **Plugins**: see #3 (separate finding).
- **Concurrent fragments**: `concurrent_fragment_downloads=1` (also a sqlite write-contention guard per M1 §4).

ffmpeg subprocess CAN still be spawned by yt-dlp's automatic merger if a single audio format isn't available and yt-dlp falls back to muxing video+audio (unlikely for `bestaudio[ext=m4a]/...`). If that happens, the cmd is constructed from format URLs and the absolute `outtmpl` (path traversal already blocked); ffmpeg-as-subprocess is itself a hardening concern (sandboxing the ffmpeg binary) but out of M1's scope. **Verdict: clean for the scope of PR #2.**

---

### 12. Tests do not hit the network

**Severity**: (informational — no finding)
**What**: `test_ytdlp.py` mocks `_sync_extract` and (where it must exercise the inner layer) `YoutubeDL` itself via `unittest.mock.patch`. The test file has no imports of `requests`/`httpx`/`urllib.request`/`socket`. Confirmed by grep across `tests/` and `src/`. `test_url_validator.py` tests pure-function string validation. **Verdict: clean.**

---

### 13. Dependency surface — no changes

**Severity**: (informational — no finding)
**What**: `pyproject.toml` and `uv.lock` are unchanged in this PR (`git diff main..HEAD` is empty for both). `yt-dlp>=2026.3.17` was already a declared dep in PR #1. The wrapper imports only from `yt_dlp` (`YoutubeDL`, `yt_dlp.utils.DownloadError`) and stdlib (`asyncio`, `logging`, `re`, `dataclasses`, `pathlib`, `typing`, `urllib.parse`). Tests import only stdlib + `pytest` + `yt_dlp.utils.DownloadError`.

Pinned yt-dlp version live in this venv: `2026.03.17`. Floor pin only, per M1 §6 item 8 ("rely on uv lock"); follow-up issue for renovate/dependabot is already noted in the plan. **Verdict: clean.**

---

### 14. Validator not enforced inside the wrapper (defense-in-depth gap)

**Severity**: LOW
**What**: `resolve_track`, `resolve_playlist`, and `download` all accept arbitrary URL strings and pass them straight to yt-dlp. The plan deliberately puts URL validation in the `/play` command call site ("BEFORE yt-dlp sees the URL", M1 §6 item 3). If a future code path forgets to call `is_supported_url` first, the wrapper will happily resolve a non-allowlisted host.
**Where**: `src/ryzic/ytdlp.py:218, 230, 257`.
**Why it matters**: One missed call site at the consumer becomes a privilege bypass. The wrapper has the validator one import away.
**Fix**: at the top of `resolve_track`, `resolve_playlist`, and `download`, `from .url_validator import is_supported_url; if not is_supported_url(url): raise FetchFailed("unsupported URL")`. Add a test per function asserting non-YouTube URLs are rejected without invoking yt-dlp. Cost: 3 lines + 3 tests; payoff: closes the entire class of "callsite forgot to validate" bugs.

---

### 15. `entry["url"]` from playlist entries is trusted, not re-validated

**Severity**: LOW (related to #14)
**What**: `_entry_from_flat` reads `entry.get("url")` from yt-dlp's flat playlist response and uses it as `TrackInfo.url`. A malicious playlist could (in principle) list arbitrary URLs as its entries' webpage URLs. The downstream `/play` consumer would then call `resolve_track(track.url)` per entry.
**Where**: `src/ryzic/ytdlp.py:181`.
**Why it matters**: If `resolve_track` doesn't re-run `is_supported_url` (see #14), yt-dlp would resolve whatever the playlist author chose. The fallback `f"https://youtu.be/{video_id}"` is safe because `video_id` is regex-validated.
**Fix**: subsumed by #14 — if `resolve_track` enforces `is_supported_url`, this is closed.

---

### 16. Test naming nit — friendly-error test message assertion

**Severity**: LOW (test fragility)
**What**: `test_resolve_track_unknown_download_error_passes_first_line` asserts `"ERROR" in msg`. This works because the test's `raw` string starts with `"ERROR: yt-dlp something went wrong"`. If yt-dlp ever changes its error prefix (no longer "ERROR:"), the test would still pass on this synthetic input but mask a real downstream change. Minor.
**Where**: `tests/test_ytdlp.py:188-198`.
**Fix** (optional): assert against the exact first-line string `"ERROR: yt-dlp something went wrong"`, or drop the substring check entirely (the multi-line stripping is what's being tested, and that's already covered by the absence of "second line"/"third").

---

## Summary

| Severity | Count |
| --- | ---: |
| HIGH (merge-blocking) | 0 |
| MEDIUM (fix soon) | 1 |
| LOW (nit) | 6 |

The single MEDIUM is **#3 (yt-dlp plugin auto-loading)** — a one-line hardening (`plugin_dirs: []`) that closes a defense-in-depth gap not enumerated in M1 §6. Worth fixing in this PR or as an immediate follow-up before the wrapper is wired into `/play`. The LOWs are all defense-in-depth or polish; none are exploitable from a Discord-user-supplied URL given the current call surface.

The brief's primary attack vectors — URL-allowlist bypass via `youtube.com.evil.com` / userinfo / IDN / control characters / scheme downgrade; argv injection; path traversal via `outtmpl` or `dest`; livestream DoS — are all defended, well-tested (67 tests, all passing in 0.10 s, mocked-network), and reflect the M1 plan's stated security model.

## Verdict

**fixes recommended** — not merge-blocking. Address #3 (MEDIUM) in this PR or as the next immediate commit on the branch; #6, #10, #14 should land before PR #3 (`/play` integration); #2, #4, #8, #15, #16 are optional polish.
