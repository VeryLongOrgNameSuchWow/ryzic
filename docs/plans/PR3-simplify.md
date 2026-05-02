# PR #3 Simplification Pass — `feat(audio): lavalink.py wire-up + voice bridge`

**Branch:** `feat/lavalink-wireup` -> `main`
**Diff reviewed:** +837 LOC across 5 files (~540 LOC src, ~300 LOC tests).
**Companion docs:** `docs/plans/M1-simplify.md` (already-decided plan-level cuts), `docs/plans/M1.md` §7 (PR5 spec), `docs/plans/M1-review.md` §6 (voice handshake race fix).

This pass looks only for code that's *more elaborate than its M1 §7 surface justifies*. It does **not** re-litigate decisions already locked in `M1-simplify.md` (module-level state dicts vs `GuildState` registry, the two-listener bridge being mandated by the libs' shape). It does not critique correctness or security — those are other agents' lanes.

Honest framing: PR3 is **already pretty tight**. There's no big subsystem to delete. The wins below are small, mostly in cosmetic indirection (`_make_starter`, `_clear_player_queue`'s getattr-guard, voice-ready event registry) and a few obviously WHAT-narrating comments. Realistic floor of LOC saved: **~30–45 LOC of source + ~10–20 LOC of tests/comments**, none of which changes the surface API or risks correctness regressions.

---

## Findings

### S-1 — Inline `_make_starter` factory into `main()`

**What to cut/collapse:** `_make_starter` is a 10-line factory (including the `TYPE_CHECKING` block + import + signature line) that returns a closure. The user is right that hikari's `subscribe` rejects lambdas — but the simpler shape is to define the `async def` directly in `main()` and pass it to `subscribe`. No factory layer; hikari's check passes the same way.

```python
async def _on_starting(_: hikari.StartingEvent) -> None:
    await client.load_extensions("ryzic.commands.lltest")
    await client.start()

bot.subscribe(hikari.StartingEvent, _on_starting)
bot.subscribe(hikari.StoppingEvent, client.stop)
```

The `TYPE_CHECKING` import block (`Callable`, `Coroutine`) goes away since the only thing that needed it was `_make_starter`'s return-type annotation.

**Where:** `src/ryzic/bot.py` lines 6, 14–15, 39–48, 69.

**Why safe:** Identical runtime behavior. `iscoroutinefunction` accepts `async def` defined inside any function body — the closure captures `client` from `main()`'s scope, same as the factory does. The factory is only justified if `_on_starting` were defined at module scope (where it would need parameters injected). At call site, factory is pure ceremony.

**Could `client.start` be subscribed directly without wrapping?** Not in this PR — the wrapper does **two** things: load the `/lltest` extension *then* start the client. Once `/lltest` is removed (PR6b per `M1-simplify.md` §7) and the package-load happens on `client` construction or via `load_extensions_from_package`, you could likely subscribe `client.start` directly. But that's a PR6b move, not PR3.

**Estimated LOC saved:** ~7 LOC (factory + the `TYPE_CHECKING`-guarded `Callable/Coroutine` import block).

---

### S-2 — Drop the getattr-guard in `_clear_player_queue`; cast like `on_track_stuck` does

**What to cut/collapse:** `_clear_player_queue` defends against a `BasePlayer` lacking a `queue` attribute via `getattr(player, "queue", None)`. But the project always uses `DefaultPlayer` (verified in `lavalink/player.py:50,122` — `self.queue: List[AudioTrack] = []`). The defensive shape exists only because the lavalink event objects type the player as `BasePlayer`. The rest of the file already handles this exact mismatch with a direct cast (line 245: `cast(lavalink.DefaultPlayer, event.player).skip()`). Be consistent and inline the call:

```python
# At each of the 2 call sites (lines 274, 295):
cast(lavalink.DefaultPlayer, event.player).queue.clear()
cast(lavalink.DefaultPlayer, player).queue.clear()
```

Drop the helper + its docstring + the two tests pinning the defensive branch (`test_clear_player_queue_handles_missing_attribute`, `test_clear_player_queue_clears_when_present`).

**Where:** `src/ryzic/lavalink_glue.py` lines 144–152, 274, 295. `tests/test_lavalink_bridge.py` lines 285–299.

**Why safe:** The project owns the player type (`DefaultPlayer` is the lavalink.py default and never overridden in this codebase). Defensive code at an internal trust boundary is the exact thing the maintainer's "no premature abstraction" rule warns against. The cast already exists once; extending the same pattern is consistent. If someone later wires a custom `BasePlayer` subclass, it's a 1-line fix at the cast site, not an obscure helper.

**Estimated LOC saved:** ~10 LOC src + ~15 LOC tests = ~25 LOC.

---

### S-3 — Replace `_voice_ready_events` dict with a per-guild `Future` (or just inline the `setdefault`)

**What to cut/collapse:** The voice-ready primitive is currently:

- `dict[int, asyncio.Event]`
- a `_voice_ready_event(guild_id)` getter that lazy-creates
- a `_reset_voice_ready(guild_id)` that pops the entry
- `wait_for_voice_ready` calling `event.wait()` with timeout

The handshake is a **one-shot signal**: set once when our own `VoiceStateUpdateEvent` arrives, then either consumed by `/play` or popped on disconnect. `asyncio.Event` is fine, but the `_voice_ready_event` lazy-getter is the indirection worth cutting — `dict.setdefault(guild_id, asyncio.Event())` at the two call sites is the canonical Python idiom (same logic, no helper):

```python
# In _on_voice_state_update (line 374):
_voice_ready_events.setdefault(event.state.guild_id, asyncio.Event()).set()

# In wait_for_voice_ready (line 89):
event = _voice_ready_events.setdefault(guild_id, asyncio.Event())
```

`Future` is **not** a win here — `Future.set_result()` raises `InvalidStateError` on a second call (the bot can rejoin/reconnect repeatedly), so callers would need the same guard logic plus a recreate. Stick with `Event`; just delete the helper.

**Where:** `src/ryzic/lavalink_glue.py` lines 73–78, 89, 374.

**Why safe:** `dict.setdefault` is the textbook "lazy create entry" pattern; replacing one wrapper with the stdlib idiom doesn't change semantics. `_reset_voice_ready` stays (it's called from 3 places and `pop(..., None)` is concise enough as a 1-line helper, but you could equally inline that too — judgment call).

**Estimated LOC saved:** ~7 LOC (delete helper + its docstring; possibly 3 more if `_reset_voice_ready` also gets inlined).

---

### S-4 — Share a small `_log_track_event` helper between `TrackEndEvent` and `TrackExceptionEvent`

**What to cut/collapse:** Both handlers (and `TrackStuckEvent`) repeat the same `track = event.track; title = track.title if track is not None else "<unknown>"` shim, then log with similar fields. The repetition is on the threshold of "three is fine" per the maintainer's rule — but the title-extraction is a real WET pattern that appears **four** times (TrackStart, TrackEnd, TrackException, TrackStuck). One module-level helper:

```python
def _track_title(track: lavalink.AudioTrack | None) -> str:
    return track.title if track is not None else "<unknown>"
```

Each handler's first 2 lines collapse to 1: `title = _track_title(event.track)`.

The audio-cache release shim noted in the `TrackEndEvent` comment (lines 200–205) and the `TrackExceptionEvent` TODO (line 222) are explicitly deferred to PR6a — once they land, a `_release_track_cache(track)` helper will earn its keep across both handlers. **Don't add it now** (no audio_cache module to import yet); just leave the existing notes.

**Where:** `src/ryzic/lavalink_glue.py` lines 185–186, 192–193, 213–214, 237.

**Why safe:** A 1-line title-extraction helper called four times is a clean refactor — the maintainer's "three similar lines" rule is satisfied (4 > 3). It doesn't introduce a class, a module, or any framework concept. If the lavalink lib ever changes the optional shape of `track`, there's now one place to update.

**Estimated LOC saved:** ~6 LOC (replace 4× 2-line shims with 4× 1-line calls + 2-line helper definition).

---

### S-5 — Drop `LavalinkNotReadyError`; raise `RuntimeError` directly

**What to cut/collapse:** `LavalinkNotReadyError` is a 2-line subclass of `RuntimeError` raised by exactly one site (`_lavalink_client_factory`, line 427). Caught nowhere. The lightbulb DI registry will surface whatever the factory raises as a `DependencyResolutionError`; downstream code never `except LavalinkNotReadyError` anywhere. Per the project convention noted in the brief — custom exceptions need a real reason — this one has none beyond a marginally nicer name in the traceback.

```python
def _lavalink_client_factory() -> lavalink.Client:
    if _ll_client is None:
        raise RuntimeError("Lavalink client requested before ShardReadyEvent fired.")
    return _ll_client
```

The one test that pins it (`test_lavalink_factory_raises_before_bootstrap`) becomes `with pytest.raises(RuntimeError):`.

**Where:** `src/ryzic/lavalink_glue.py` lines 421–422, 427. `tests/test_lavalink_bridge.py` line 241.

**Why safe:** `errors.py` is the project's home for caught/typed domain exceptions (per `M1-simplify.md` "Things I considered cutting and decided not to"). `LavalinkNotReadyError` lives in `lavalink_glue.py`, not `errors.py` — telling the same story: nobody catches it. If PR6a introduces a `/play` flow that genuinely needs to distinguish "lavalink not ready" from other RuntimeErrors, add it then; YAGNI for PR3.

**Estimated LOC saved:** ~3 LOC.

---

### S-6 — Make `wait_for_voice_ready` internal (`_wait_for_voice_ready`) until PR6a needs the public surface

**What to cut/collapse:** `wait_for_voice_ready` is exported as public (no leading underscore), but no caller in PR3 uses it — `/lltest` doesn't need it, and `/play` lands in PR6a. The function is well-named and its docstring is good, but until PR6a wires it up, it's a public surface awaiting a consumer. Rename to `_wait_for_voice_ready` for now; PR6a flips the underscore when it imports it. (Or: leave it public — it's fine either way; this is the weakest finding.)

**Where:** `src/ryzic/lavalink_glue.py` line 81.

**Why safe:** Cosmetic — no LOC change, just signaling that this is an unfinished surface. **Optional**, low-conviction. I'd actually recommend leaving it: the docstring documents intent and the tests pin behavior; making it public now means PR6a doesn't need to touch this file at all. Pulling it back to private only to re-promote it is churn.

**Estimated LOC saved:** 0. **Recommend skipping.**

---

### S-7 — `auto_leave_tasks` -> `loop.call_later(...)` returning `TimerHandle`

**What to cut/collapse:** Considered: replace the `dict[int, asyncio.Task[None]]` + the `_auto_leave` coroutine with `asyncio.get_event_loop().call_later(300, callback, ...)` returning a `TimerHandle`, which is `cancel()`-able. The current shape spawns a task that just `await sleep(300)` then does work — that's literally what `call_later` is for.

**Why I'm not recommending it:** The work after the sleep is **async** (`await bot.update_voice_state(...)`, `await _send_to_last_play_channel(...)`). `call_later`'s callback must be a sync function — to call async code you'd need `loop.create_task(_do_disconnect(...))` inside the callback, which puts you right back to needing a tracked task. The `dict[int, Task]` shape with `_cancel_auto_leave` is the right primitive here; `TimerHandle` would be the same LOC plus an extra layer.

**Where:** `src/ryzic/lavalink_glue.py` lines 102–141.

**Estimated LOC saved:** 0. **Recommend skipping** — the current shape is already minimal for the async-after-delay pattern. The auto-leave task is named (`name=f"ryzic-auto-leave-{guild_id}"`) which makes traces readable; that disappears with `call_later`.

---

### S-8 — Test scaffolding richer than the surface

**What to cut/collapse:** Two specific spots:

- **`test_bridge_voice_server_payload_does_not_substring`** (lines 129–137) duplicates `test_bridge_voice_server_payload_strips_wss_scheme` (lines 114–120). Both assert that `removeprefix("wss://")` works correctly; the second one's docstring says it "pins behaviour so a regression silently corrupting hosts is caught" — but the first test already does that. The second test only adds value if it tested the **negative** case (`endpoint="https://example.com"` should NOT be stripped to `example.com`). As written it's a redundant happy-path. Either delete it or rewrite to test the `https://` case (which is what the comment is gesturing at).
- **`test_clear_player_queue_handles_missing_attribute` + `test_clear_player_queue_clears_when_present`** (lines 285–299) — both go away as part of S-2 above.

**Where:** `tests/test_lavalink_bridge.py` lines 129–137 (~9 LOC), 285–299 (~15 LOC, already counted in S-2).

**Why safe:** Each removed test has either a duplicate doing the same job or a removed function to test.

**Estimated LOC saved:** ~9 LOC (the duplicate); the other 15 LOC is already in S-2.

---

### S-9 — `/lltest` placement: keep as separate file, not inline

**What to cut/collapse:** Considered: collapse `commands/lltest.py` into `lavalink_glue.py` since it's throwaway anyway.

**Why I'm not recommending it:** Lightbulb v3's extension model **requires** commands to live in a module loaded via `client.load_extensions(...)` so that the `Loader` decorator's `@loader.command` registration runs at import time (per `lightbulb/client.py:668–717` — the loader walks the imported module looking for `Loader` instances). Inlining into `lavalink_glue.py` would require also exposing a `loader` from that module and loading `lavalink_glue` as an extension, which entangles the bridge code with the slash-command framework's lifecycle. The 60-line throwaway file is the right shape; PR6b deletes it cleanly per the plan.

**Where:** `src/ryzic/commands/lltest.py`.

**Estimated LOC saved:** 0. **Recommend skipping.**

---

### S-10 — Trim WHAT-narrating comments

**What to cut/collapse:** Several comments narrate WHAT the next line does without adding intent. Specific cuts:

- `lavalink_glue.py:60–61` — `# Singleton lavalink client. Created once on the first ``ShardReadyEvent`` ... ``None`` until then; bridge listeners short-circuit while we wait.` This restates the type annotation (`lavalink.Client | None`) and the if-None guards visible 30 lines below. Drop to one line: `# Constructed on first ShardReadyEvent (needs the bot's user id).`
- `lavalink_glue.py:411` — `# Idempotent across calls is NOT guaranteed; call once at startup.` This one is **WHY**, keep it.
- `lavalink_glue.py:438–440` — the `# Test seams` ASCII separator. The function names already start with `_` and have `_for_test` suffix; the visual divider is decorative. Cut.
- `bot.py:43–44` — `# Extensions register commands; commands are synced inside ``client.start``, so load before starting.` This is **WHY** (call ordering matters), keep it.

**Where:** `src/ryzic/lavalink_glue.py` lines 59–61, 438–440.

**Why safe:** WHY-comments and load-bearing intent stay. Only ASCII decoration and restated-from-context narration go.

**Estimated LOC saved:** ~5 LOC.

---

## Top 3 wins by impact

1. **S-2 (drop `_clear_player_queue` getattr-guard, cast like the rest of the file)** — ~25 LOC, removes a defensive-at-internal-boundary helper, and makes the file's `BasePlayer`-vs-`DefaultPlayer` story consistent (already cast for `.skip()`; do the same for `.queue`).
2. **S-1 (inline `_make_starter`)** — ~7 LOC, removes a factory + the `TYPE_CHECKING` import block. The factory exists only to placate `iscoroutinefunction`'s lambda rejection; an inline `async def` solves the same problem without a layer.
3. **S-4 (`_track_title` helper across 4 handlers)** — ~6 LOC, the only finding that *adds* a tiny abstraction, and only because the WET pattern is at 4 sites (not 3) and the lavalink lib's `event.track` is genuinely `Optional`.

---

## Keep as-is

- **The two-listener voice-update bridge.** Mandated by the lib shapes, called out in `M1-simplify.md` "Things I considered cutting and decided not to". Nothing to simplify.
- **`auto_leave_tasks: dict[int, asyncio.Task[None]]`.** `TimerHandle`/`call_later` would be worse for async-work-after-delay; the named task makes traces readable. (S-7 considered and rejected.)
- **`/lltest` as a separate file.** Lightbulb v3's `Loader` model requires the command in its own importable extension module; inlining would entangle `lavalink_glue.py` with the command framework. (S-9 considered and rejected.)
- **`wait_for_voice_ready` as public.** PR6a will import it; flipping the underscore now is churn. (S-6 considered and rejected.)
- **`_bridge_voice_server_payload` / `_bridge_voice_state_payload` extracted.** The module docstring justifies it: tests can exercise the dict translation without spinning up a real lavalink client. Worth the 2 small helpers.
- **The module docstring.** Long but every paragraph carries a non-obvious WHY (handshake race, why `play()` from `TrackEndEvent` is wrong, `removeprefix` over `[6:]`). Keep as-is.
- **`_set_lavalink_client_for_test` / `_reset_state_for_test` test seams.** Module-global state needs explicit test reset; these are minimum-viable.
- **`_FakeApp` test fixture.** Stand-in for `hikari.GatewayBot` is necessary because hikari's events are tightly bound to the gateway machinery; mocking it ad-hoc per test would be more code, not less.

---

## Total impact

- **LOC saved (source):** ~30–35 (S-1: 7, S-2-src: 10, S-3: 7, S-4: 6, S-5: 3, S-10: 5; net of S-4 adding 2 lines for the helper).
- **LOC saved (tests):** ~25 (S-2-tests: 15, S-8: 9).
- **Total LOC: ~55–60 across 837 added,** about 7%. PR3 is **already pretty tight**; no big subsystem to delete.

---

## Report

(a) File saved at `/home/user/Projects/ryzic/docs/plans/PR3-simplify.md`.

(b) Top 3:
   1. S-2 — drop `_clear_player_queue` getattr-guard + the two tests pinning it (~25 LOC).
   2. S-1 — inline `_make_starter` factory into `main()` (~7 LOC + `TYPE_CHECKING` block).
   3. S-4 — `_track_title` helper across 4 EventHandler hooks (~6 LOC).

(c) Total LOC the PR could lose: **~55–60 LOC** (out of +837), most of which is removing one defensive helper and its tests. The PR is mostly tight; no structural rewrites are warranted.
