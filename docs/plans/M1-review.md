# M1 Plan — Review & Critique

Reviewer: fork of main agent, asked to be ruthless. **Overall: minor revisions.** The plan is solid, the security review is unusually thorough, and the Lavalink seam is well-scoped. Below are concrete issues to address.

---

## Severity ranking (top 5)

| # | Severity | What | Where |
|---|---|---|---|
| 1 | **HIGH** | URL validator regex `(youtube\.com\|youtu\.be)/` matches `youtube.com.evil.com/...` — domain is not anchored | §3 `/play`, §6 |
| 2 | **HIGH** | No livestream rejection — yt-dlp will infinitely download a live URL until cache fills | §6 |
| 3 | **HIGH** | "Bot already in different channel" UX tells user to `/leave`, but `/leave` requires same channel — instruction is impossible | §3 `/play` |
| 4 | **MEDIUM** | PR6 (~500 LOC for 6 commands + ux.py + hook + error handlers) will overflow — realistic 700–900 LOC | §12 |
| 5 | **MEDIUM** | Cross-cutting "same voice channel" lightbulb hook with two-command exclusion list is more complex than inline helper calls | §3 |

---

## 1. KISS / DRY / SRP / SOLID

### `errors.py` lists too many domain exceptions

**What:** `UserNotInVoice`, `CacheMiss`, `FetchFailed`, `...` — `UserNotInVoice` is a state check, not an exception worth raising and catching across module boundaries.
**Where:** §2 file layout
**Why it matters:** Raise-then-catch-at-the-boundary works for one or two domain errors; with five+ it becomes a parallel control-flow system. Each command writer has to learn the exception ladder.
**Suggested fix:** Cut to two: `FetchFailed` (yt-dlp/lavalink failures) and `InvalidVideoID` (path safety). Voice-state checks happen inline in command handlers and return early with the ephemeral string.

### `state.py` `GuildStateRegistry` is ceremony around a dict

**What:** Two fields (`voice_channel_id`, `last_play_channel_id`), one of which is derivable from `bot.cache.get_voice_state(guild_id, bot_user_id)`.
**Where:** §2, §8
**Why it matters:** A `dict[int, int]` for `last_play_channel_id` is sufficient. The dataclass + registry pattern adds two indirections for one value.
**Suggested fix:** Drop `GuildState` and `GuildStateRegistry`. Store `last_play_channel: dict[int, int]` directly in `lavalink_glue.py`. Read `voice_channel_id` from hikari cache when needed.

### Cross-cutting voice-channel hook with exclusion list

**What:** §3 says "one shared lightbulb hook: all commands except `/play` and `/queue` require user in same voice channel." That's command-name introspection inside a hook.
**Where:** §3 cross-cutting check
**Why it matters:** Hooks that branch on command name encode policy in the hook instead of the command. Six commands, two exclusions = the hook has more "if" than logic.
**Suggested fix:** A tiny helper `await ensure_same_voice(ctx) -> bool | int` (returns channel_id or sends ephemeral and returns False). Each command needing the check calls it as the first line. Explicit > clever.

### `cache/` and `ytdlp/` as packages with two files each

**What:** `cache/audio.py` + `cache/playlist.py` and `ytdlp/wrapper.py` + `ytdlp/models.py`.
**Where:** §2
**Why it matters:** A package-with-two-files is a directory you'll add an `__init__.py` to and re-export through. For M1, `cache_audio.py` + `cache_playlist.py` and `ytdlp.py` (with models inline) at the top level test exactly as well and read more directly.
**Suggested fix:** Flatten to `audio_cache.py`, `playlist_cache.py`, `ytdlp.py`. Reconsider packaging if the modules grow past ~300 LOC each post-M1.

### `YtDlpService.resolve` returns `TrackInfo | PlaylistInfo`

**What:** Union return type forces every caller to `isinstance` branch.
**Where:** §6
**Why it matters:** SRP: a method should answer one question. Track resolution and playlist resolution are different operations triggered by different URL shapes.
**Suggested fix:** Split into `resolve_track(url) -> TrackInfo` and `resolve_playlist(url) -> PlaylistInfo`. URL-shape detection (presence of `list=` query param) happens in `/play` before dispatching.

---

## 2. Premature abstractions

### `commands/__init__.py` "extension loader"

**What:** Listed as a separate concern.
**Where:** §2
**Why it matters:** lightbulb v3 has `client.load_extensions_from_package(...)` which auto-discovers. An explicit loader is unneeded unless gating is required.
**Suggested fix:** Empty `__init__.py`; load via `client.load_extensions_from_package(ryzic.commands)`.

### Lock dict ref-counting cleanup

**What:** §4 "Lock dict cleanup: when a lock is released and no one else is waiting, pop it (use `asyncio.Lock` ref counting via context manager wrapper)."
**Where:** §4 audio cache
**Why it matters:** Premature optimization. Per-video locks are a few bytes each; even 100k cached videos = trivial RAM.
**Suggested fix:** Leak the locks for M1. Add a one-line comment noting the deliberate trade-off. Revisit if it ever matters (it won't).

### `__main__.py` separate from `bot.py`

**What:** `__main__.py` is a one-liner; the printed version stub is excess for PR1.
**Where:** §2, PR1 (§12)
**Why it matters:** Two-file entrypoint for a single-line `from .bot import main; main()` is fine, but the "print version" stub for PR1 is meaningless ceremony.
**Suggested fix:** Keep `__main__.py` (it's idiomatic for `python -m`), drop the stub-printing-version step from PR1's scope.

---

## 3. Missing edge cases in command UX

### Bot-already-in-different-channel UX is self-contradicting

**What:** §3 `/play` "Bot already in a different voice channel": ephemeral `"I'm already playing in <#{other_id}>. Have someone use /leave first or join that channel."` But `/leave` requires user in same channel as bot.
**Where:** §3 `/play`
**Why it matters:** Tells user to invoke a command they can't successfully invoke. UX dead-end.
**Suggested fix:** Drop the `/leave` suggestion: `"I'm already in <#{other_id}>. Join that channel first."` Or relax `/leave` to allow anyone to invoke (which is what this scenario actually wants).

### Livestream URLs not detected → infinite download

**What:** yt-dlp resolves YouTube livestream URLs. Cache-first design assumes a finite file size. A live URL downloads forever.
**Where:** §6
**Why it matters:** Trivial DoS. One `/play` with a live URL fills the disk.
**Suggested fix:** After `extract_info`, check `info.get("is_live")` and `info.get("live_status") in {"is_live", "is_upcoming"}`. Reject before download with `"Livestreams are not supported in this version."` Add to security review §6 too.

### Queue overflow

**What:** No cap on queue size.
**Where:** §3, §7
**Why it matters:** A user can queue multiple 1000-track playlists; queue grows unbounded; `/queue` rendering slows; memory pressure.
**Suggested fix:** Cap queue at e.g. 500 tracks. New `/play` that would overflow shows `"Queue is full ({N}/{MAX}). Wait for some tracks to finish."`

### Discord interaction 15-minute timeout vs slow yt-dlp

**What:** `/play` defers; if yt-dlp takes >15 min (huge playlist), interaction dies and the user sees nothing.
**Where:** §3 `/play`
**Why it matters:** Discord deletes the deferred response; user waits with no feedback; eventually wonders if it worked.
**Suggested fix:** For playlists, fast-fail if `>1000` tracks (already specified in §6 `playlist_items` cap — make this UX-visible). For single tracks, document that the playable sources have practical timing limits and rely on the 1000-cap.

### `/play` while paused

**What:** Plan doesn't say whether `/play` unpauses.
**Where:** §3
**Why it matters:** Ambiguity → inconsistent behavior across implementer agents.
**Suggested fix:** Specify: `/play` does NOT unpause. New tracks join the queue; user must `/resume` to play.

### `/play` from DM (no guild)

**What:** Plan doesn't handle DM context.
**Where:** §3 `/play`
**Why it matters:** lightbulb command without `dm_enabled=False` accepts DM context where most code paths assume a guild.
**Suggested fix:** Configure all commands with `dm_enabled=False` (lightbulb v3 supports per-command DM gating). One-line config.

### Stage channel handling

**What:** Plan doesn't say whether the bot supports stage channels.
**Where:** §3 `/play`
**Why it matters:** Stage channels need the bot to be invited as speaker. Joining as audience and trying to `Speak` errors. UX should reject clearly.
**Suggested fix:** Detect stage channel via `channel.type == hikari.ChannelType.GUILD_STAGE`; ephemeral `"Stage channels aren't supported. Use a regular voice channel."`

### `/queue` doesn't indicate paused state

**What:** `"{pos_mm:ss} / {len_mm:ss}"` doesn't say "paused".
**Where:** §3 `/queue`
**Why it matters:** User sees frozen progress and can't tell if it's paused or stuck.
**Suggested fix:** Append `" (paused)"` to the progress line when `player.paused`.

### Markdown injection in titles via `/queue` and Now Playing

**What:** `[{title}]({url})` markdown link breaks if title contains `[`, `]`, `(`, `)`.
**Where:** §3 `/queue`, §3 `/play` "Queued" embed
**Why it matters:** Garbled embeds for videos with brackets in titles (very common — `[Official Video]` etc.). Possibly clickable-text injection.
**Suggested fix:** Escape `[`, `]`, `(`, `)` in titles before formatting as markdown link, OR use Discord's plain-text formatting and put the URL in an embed field instead of inline.

### Voice channel deleted while bot is in it

**What:** No flow.
**Where:** §3, §7 EventHandler hooks
**Why it matters:** `WebSocketClosedEvent` (4014) fires; plan only logs and clears state. User who started playback gets no notification.
**Suggested fix:** On 4014, post in `last_play_channel_id`: `"Voice connection lost. Queue cleared."` and clear queue.

### `/play` for age-restricted content

**What:** yt-dlp errors with cryptic "Sign in to confirm your age" — passed through verbatim.
**Where:** §3 `/play`, §6
**Why it matters:** Confusing error. Self-hoster gets bug reports about login they can't fix without cookies (which we don't support).
**Suggested fix:** Map known yt-dlp error patterns to friendlier strings: age-restricted → `"That video is age-restricted and can't be played."`; private → `"That video is private."`; region-locked → `"That video is not available in this region."`

### `/skip` while paused

**What:** Plan doesn't say.
**Where:** §3 `/skip`
**Why it matters:** Implementer freedom = inconsistency.
**Suggested fix:** Specify: skip advances queue; new track stays paused (user must `/resume`).

---

## 4. Concurrency holes in audio cache

### Eviction can race with download-completion → dangling path

**What:** `os.replace` is atomic, but `INSERT INTO entries` is a separate step. If eviction picks a not-yet-inserted file (filesystem scan) or misses one (sqlite scan only), inconsistency.
**Where:** §4
**Why it matters:** Either the just-downloaded file is silently deleted (causing immediate "Lavalink LOAD_FAILED") or orphan files accumulate.
**Suggested fix:** Be explicit: "Eviction queries sqlite only. Filesystem files without sqlite rows are orphans. Startup runs an orphan-sweep that deletes any audio files lacking sqlite rows older than 1 hour."

### Just-downloaded file evictable before Lavalink loads it

**What:** `_in_use` is set when Lavalink loads; download completes earlier; eviction window between these two points.
**Where:** §4
**Why it matters:** Race: download → evictor runs (over-budget cap exceeded by the new file itself!) → file gone → Lavalink load fails. User sees `"Audio service ..."` for a track they just queued.
**Suggested fix:** Mark `_in_use[video_id] += 1` *before* returning the path from `get_or_download`. Decrement on `TrackEndEvent` or after a failed Lavalink load.

### `_in_use` leaks on missed `TrackEndEvent`

**What:** `WebSocketClosedEvent`, bot crash, Lavalink restart — `TrackEndEvent` may never fire.
**Where:** §4, §7
**Why it matters:** Entries accumulate in `_in_use` indefinitely; eviction skips them; cache fills up; eventually wedges.
**Suggested fix:** On bot startup, clear `_in_use` (everything's restartable). On `WebSocketClosedEvent` / `QueueEndEvent`, decrement all entries owned by the relevant guild. Use a counter (`Counter[str]`) not a set, since the same video can be queued multiple times.

### sqlite write contention

**What:** Multiple guilds writing concurrently.
**Where:** §4
**Why it matters:** SQLite's default journal mode serializes writes. Music-bot scale is fine but worth being explicit.
**Suggested fix:** Open sqlite with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. Use `aiosqlite` (or wrap sync sqlite in `asyncio.to_thread`).

---

## 5. Security gaps (additions to §6)

### URL validator allows `youtube.com.evil.com`  ← **HIGH**

**What:** Regex `(youtube\.com|youtu\.be)/` is not anchored at the host boundary. `https://youtube.com.evil.com/anything` matches because the domain alternation isn't preceded by an end-of-host anchor.
**Where:** §3 `/play`, §6 (item 3)
**Why it matters:** yt-dlp will be called with attacker-chosen URLs that aren't actually YouTube. This subverts the entire URL-allowlist defense. yt-dlp's extractor will probably still bounce most of these but you've broken the contract.
**Suggested fix:** Use `urllib.parse.urlparse(url)` and check `parsed.hostname` against an exact set: `{"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}`. Also reject `parsed.scheme != "https"`.

### `http://` accepted

**What:** Regex `https?://` allows non-TLS.
**Where:** §3 `/play`
**Why it matters:** MITM risk on the URL fetch itself; minor.
**Suggested fix:** Drop the `?` — require `https://`.

### Discord markdown injection in error embeds

**What:** yt-dlp error string may contain backticks; wrapping in inline code (`` `{...}` ``) doesn't escape internal backticks.
**Where:** §3 `/play` (URL fetch failed message)
**Why it matters:** Attacker-controlled video page metadata could break out of the inline code formatting and inject markdown.
**Suggested fix:** Strip backticks from error string before wrapping, OR put the error in a code-block (triple-backtick) and strip triple-backticks specifically.

### Embed length limits not enforced

**What:** Discord embed fields have hard limits (description 4096, field value 1024, footer 2048). yt-dlp errors, video titles, playlist titles can exceed.
**Where:** §3 (all embed builders)
**Why it matters:** `send` raises `BadRequest`; command falls into generic error handler; user sees nothing useful.
**Suggested fix:** Truncate at the boundary in `ux.py`. Document limits in a single helper.

### Lavalink local file source — future expansion risk

**What:** Currently we pass `cache_root / video_id[:2] / video_id.ext` — fully validated. Plan should explicitly forbid future code from passing user-influenced paths.
**Where:** §4, §7
**Why it matters:** A future "play this URL" extension that lets the user hint at file type (or any other path-influencing input) becomes a Lavalink container file-read primitive.
**Suggested fix:** Add to plan: "All paths passed to Lavalink MUST be derived from a validated `video_id`. New code paths must extend `_validate_path(p)` which enforces `p.relative_to(cache_root)`."

### Cookies disabled but not documented

**What:** Plan doesn't mention cookies. yt-dlp default is no cookies, but `cookiefile` env-var hooks exist.
**Where:** §6
**Why it matters:** Future maintainer "fixes" age-restricted by mounting a `cookies.txt`, leaks the host's YouTube session.
**Suggested fix:** Explicit `cookiefile: None` in the opts dict. Comment: "Do not enable; out of scope for M1 and security-sensitive."

---

## 6. Lavalink integration — additional risks

### `event.endpoint[6:]` is fragile

**What:** Hardcoded slice for stripping `wss://` prefix.
**Where:** §7 voice-update bridge
**Why it matters:** If Discord ever returns endpoint without protocol, or with a different prefix, slices garbage. Breaks silently.
**Suggested fix:** `event.endpoint.removeprefix("wss://")`.

### Node-down (not 4014) has no recovery path

**What:** Plan handles `WebSocketClosedEvent` code 4014 (Discord-side disconnect) but not Lavalink-server-side disconnects (lavalink.py emits `NodeDisconnectedEvent`).
**Where:** §7
**Why it matters:** Lavalink container restart leaves all players orphaned. Bot appears alive but no audio works.
**Suggested fix:** Subscribe to `NodeDisconnectedEvent` and `NodeConnectedEvent`. On disconnect, mark all players invalid; on reconnect, log and let users `/play` again to recreate. Optionally post a heads-up in last-known channels.

### Voice events arriving before node ready

**What:** `ShardReadyEvent` fires *after* the bot is ready; voice events can theoretically arrive during reconnect before `ll_client` is constructed.
**Where:** §7 node bootstrap
**Why it matters:** `voice_update_handler` called on `None` → AttributeError.
**Suggested fix:** Guard the listeners: `if ll_client is None: return`. Voice events without state are recoverable — Discord re-sends on next interaction.

### `player.play()` race against voice handshake

**What:** After `bot.update_voice_state(...)`, voice events flow asynchronously. Calling `player.play()` immediately may fire before lavalink.py has the voice connection.
**Where:** §7 player lifecycle
**Why it matters:** Documented gotcha in lavalink.py issues. Plays silently nothing.
**Suggested fix:** After joining voice, `await asyncio.wait_for(_voice_ready_event.wait(), timeout=5.0)` before `player.play()`. The `_voice_ready_event` is set in the `VoiceStateUpdateEvent` listener for our own user. Or use lavalink.py's `player.is_connected` polling.

### Lavalink may return empty `LOAD_FAILED` for the local file

**What:** Lavaplayer can fail to recognize codecs, especially exotic ones yt-dlp may produce.
**Where:** §7 local file loading
**Why it matters:** Cache hit, Lavalink reject, user confused.
**Suggested fix:** Constrain yt-dlp `format` to `bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio` (priority order — known-good Lavaplayer codecs first). Fail gracefully if none available.

### Multi-guild `_in_use` Counter contention

**What:** `_in_use` shared across guilds; concurrent increment/decrement.
**Where:** §4, §7
**Why it matters:** Python dict `+=` on shared counter is technically not atomic; under uvloop fine, under asyncio default fine, but a `Counter[str]` with `+=` is two operations (read + write).
**Suggested fix:** Wrap mutations in a single coroutine via `asyncio.Lock` or use `dict.setdefault` + atomic-ish pattern. Trivial.

### `lavalink>=5.11.0` floor not verified

**What:** Plan pins floor; I haven't verified 5.11.0 exists or is the right floor.
**Where:** §1
**Why it matters:** Wrong floor blocks `uv lock`.
**Suggested fix:** Implementer agent should `uv add lavalink` and pin to whatever the latest is at that moment.

---

## 7. Test strategy gaps

### No test for the voice-update bridge

**What:** §7 listeners are pure transformations; no mention of testing them.
**Where:** §11
**Why it matters:** This is the single most-flagged risk area in the entire plan; not testing it is a contradiction.
**Suggested fix:** Two tests in `tests/test_lavalink_bridge.py`: feed mock `VoiceServerUpdateEvent` and `VoiceStateUpdateEvent`, assert the dict shape passed to a fake `voice_update_handler`. Both events together — assert ordering doesn't matter.

### No integration test for Lavalink

**What:** Plan only specifies unit tests.
**Where:** §11
**Why it matters:** The local-file-readable-by-Lavalink class of bugs (volume mount, codec, Lavalink config) is only catchable end-to-end.
**Suggested fix:** Add `tests/integration/test_lavalink_smoke.py`: testcontainer spins up Lavalink, asserts `get_tracks(local_path)` succeeds for a known-good test audio file. Mark as `@pytest.mark.integration`, skipped by default in unit-test runs, run in CI.

### No coverage target on commands

**What:** §11 lists 80% on `cache/`, `ytdlp/`, URL validator only.
**Where:** §11
**Why it matters:** Commands are where the behavior lives; tests give regression-protection on UX strings (which the plan has invested in heavily).
**Suggested fix:** Add 60% coverage target on `commands/` via mocked lightbulb context. Test the cross-cutting voice-channel check, error-mapper.

### Test runner not specified

**What:** No mention of `pytest-asyncio` or `anyio`.
**Where:** §11
**Why it matters:** lavalink.py is async-heavy; tests need a runner.
**Suggested fix:** Add `pytest>=8`, `pytest-asyncio>=0.23` to dev deps. Configure `asyncio_mode = "auto"` in `pyproject.toml`.

### Manual smoke-test checklist needs a home

**What:** §11.9 says "documented in the M1 PR description" — that's a single-PR artifact, not reusable.
**Where:** §11
**Why it matters:** Future PRs need to re-do this checklist; description-only documentation rots.
**Suggested fix:** `docs/manual-smoke-tests.md` with the checklist; PR description links to it.

---

## 8. PR breakdown sanity

### PR3 combines two domains

**What:** "audio cache + yt-dlp wrapper" in one PR.
**Where:** §12 PR3
**Why it matters:** Different abstractions, different test surfaces. ~450 LOC straddling two concerns is harder to review than two ~250-LOC PRs.
**Suggested fix:** Split:
- **PR3a — feat(ytdlp): wrapper + URL validator + tests** (~250 LOC)
- **PR3b — feat(cache): audio cache + tests** (~250 LOC, depends on PR3a for `TrackInfo`)

### PR6 will overflow ~500 LOC budget

**What:** Six commands + ux.py + cross-cutting hook + error handlers.
**Where:** §12 PR6
**Why it matters:** Realistic per-command LOC: 80–120 (defer + voice check + error paths + embed build). 6 × 100 = 600 + ux.py ~100 + helpers ~50 = 750 LOC.
**Suggested fix:** Split:
- **PR6a — feat(commands): /play + ux.py + voice-channel helper** (~400 LOC)
- **PR6b — feat(commands): /skip /queue /pause /resume /leave** (~400 LOC)

### `/lltest` throwaway in PR5

**What:** Added in PR5, removed in PR8. Wasteful but useful.
**Where:** §12 PR5, PR8
**Why it matters:** Reviewers may flag the throwaway code as missing tests.
**Suggested fix:** Mark in code with `# TEMP(PR8): smoke-test command, removed once /play exists`. Acceptable.

### PR9 docs PR before PR7 reality

**What:** "Can be drafted in parallel any time after PR1" then "finalized after PR7".
**Where:** §12 PR9
**Why it matters:** Drift risk if drafted-then-not-touched.
**Suggested fix:** PR9 starts only after PR7 lands; that's docs-on-real-code, not docs-on-plan.

### No security-review pass

**What:** User's standards mandate `/security-review` before merge.
**Where:** §12
**Why it matters:** Per project memory, this is a hard rule.
**Suggested fix:** Add explicit `/security-review` agent run as a pre-merge gate on each PR. Note in plan, not a separate PR.

### LOC estimate is low

**What:** "~2150 LOC across 9 PRs" — realistic 3000+ with CI workflows, more test code, error handlers.
**Where:** §12
**Why it matters:** Budget overrun → either bloated PRs or surprise additional PRs.
**Suggested fix:** Note: "LOC estimates are floors. Splitting allowed mid-implementation."

---

## 9. Open questions critique

### Q1 (Lavalink server version pin) — not really a question

**What:** v4 is the only viable version.
**Where:** §0
**Suggested fix:** Drop from open questions. Lock as decision.

### Q2 (cache-key collision) — answer is obvious

**What:** Collapsing equivalent URLs to one cache entry is obviously-correct.
**Where:** §0
**Suggested fix:** Drop. Document as decision in §4.

### Q3 (playlist append while playing) — sensible default

**What:** Yes, append. Easy to revisit.
**Where:** §0
**Suggested fix:** Lock default; drop from open questions.

### Q4 (single-shard) — trivially fine

**What:** Self-hosted bot; one shard.
**Where:** §0
**Suggested fix:** Drop. Document as scope.

### Missing open questions — these matter more

1. **Auto-leave on idle?** `QueueEndEvent` currently doesn't disconnect. UX-wise an idle bot in voice is awkward. Worth user input: "Do you want auto-leave after N minutes idle?" If yes, default 5 min.
2. **Lavalink image tag pin specificity?** `:4` floats. `:4.0.7` (or whatever's latest) is reproducible.
3. **Privacy stance for the cache directory?** Cached audio is technically copyrighted material on the host disk. Self-hosters should know. One-line in README.
4. **Logs to stdout (compose default) or file?** If file, volume needed.
5. **Idle disconnect from Discord (Discord disconnects inactive voice)** — should the bot self-leave first to keep state clean?

---

## 10. Future-incident watchlist

- **YouTube bot detection / PoToken**: yt-dlp regularly gets blocked by YouTube. Cache is the mitigation; document this is a known fragility.
- **Lavalink server → 5 upgrade**: protocol breaks. Pin major version (`:4`) explicitly; do not auto-bump.
- **`ty` (the type checker) is alpha**: regressions possible. Tolerate breakage.
- **`hikari-lightbulb` v3 is recent**: pin minor (`>=3.2,<4`).
- **Discord global slash command propagation = 1h**: dev iteration uses `RYZIC_GUILD_IDS`; document loudly.
- **ARM self-hosters**: confirm `ghcr.io/lavalink-devs/lavalink:4` is multi-arch (it is, but document support matrix).
- **Self-hoster runs on Windows**: `windowsfilenames: false` in yt-dlp opts may produce Linux-only filenames; the cache lives in the Linux Lavalink container so it's fine, but the local-dev `./.cache` path on a Windows dev box could fail. Doc note.
- **Disk full from non-cache files**: LRU only governs cache; if cache shares volume with logs, wedges. Doc note: dedicated volume.
- **`bot.update_voice_state(..., self_deaf=True)`**: verify hikari's default for `self_mute` (likely False — i.e. unmuted, which is what we want — but worth confirming).

---

## What the plan got right

- **Security review (§6) is unusually thorough** for an M1 plan. The argv-injection / path-traversal / disk-fill / playlist-bomb coverage is exactly the right stuff. Issues above are additions, not contradictions.
- **Lavalink hazard zone (§7) gets the documentation it deserves** — citing specific issues #144, #150, #153 with concrete mitigations is the right energy for "the place we got burned before."
- **PR breakdown's parallelization graph** is realistic — the cache layer being orthogonal to the bot layer means PR3 can be developed in a worktree with no Discord knowledge.
- **`.env` in `.gitignore` (§1) explicitly called out** — closes the very loophole that bit you 30 minutes ago.
- **Self-hoster setup (§10) acknowledges the privileged-intents trap and the Administrator-permission anti-pattern** — both real-world rookie mistakes.
- **No persistence in M1 is the right call** (§8). Queue restoration is a rabbit hole; calling it out as deliberate KISS is better than half-implementing it.

---

## Suggested next steps

1. **Patch the plan** to address H1–H3 (URL validator anchor, livestream rejection, `/play`-already-in-channel UX). Roughly 50 lines of edits.
2. **Decide the missing open questions** (auto-leave-on-idle is the only one with real UX impact; rest are technical defaults).
3. **Re-split PR3 and PR6**, accept new total of ~11 PRs.
4. Then implement.
