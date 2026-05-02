# PR #3 Review — `feat(audio): lavalink.py wire-up + voice bridge`

**Branch:** `feat/lavalink-wireup` → `main`
**Scope per plan:** `docs/plans/M1.md` §7 (lavalink.py wire-up + voice bridge — known hazard zone) and §12 PR5.
**Diff:** +837/-2 LOC across 5 files (`bot.py` +20, `commands/__init__.py` +1, `commands/lltest.py` +60, `lavalink_glue.py` +457, `tests/test_lavalink_bridge.py` +299).
**Local verification:** `uv run ruff check`, `uv run ty check`, `uv run pytest -q` — all green (30 tests, ~0.4s).

---

## Findings

### MEDIUM-1 — `LavalinkNotReadyError` is wrapped by linkd; downstream catches will not see it

- **Severity:** MEDIUM
- **Where:** `src/ryzic/lavalink_glue.py:421-435` (factory + DI wiring); the implementer's report claims "the DI factory raises `LavalinkNotReadyError` if resolved before bootstrap" and that PR6a's `/play` "should catch and respond `Audio service is down…`".
- **Why it matters:** linkd's container resolution wraps every factory exception in `DependencyNotSatisfiableException` — see `.venv/lib/python3.13/site-packages/linkd/container.py:259-268`:
  ```python
  except Exception as e:
      raise exceptions.DependencyNotSatisfiableException(
          f"could not create dependency {dependency_id!r} - factory raised exception"
      ) from e
  ```
  So a `/play` handler that does `lavalink_client: lavalink.Client = lightbulb.di.INJECTED` (the natural DI surface) will see `DependencyNotSatisfiableException` with `LavalinkNotReadyError` only in `__cause__`. The plan-mandated friendly path (plan §3: `"Audio service is down. Try again in a minute."`) requires either catching `DependencyNotSatisfiableException` (couples `/play` to the DI library's exception type, defeating the abstraction) OR walking `e.__cause__` (brittle). The PR commits the implementer to a contract that the runtime won't honor.
  Note: `/lltest` sidesteps this entirely by calling `lavalink_glue.get_lavalink_client()` directly and checking for `None` — that pattern works. The DI factory in its current form is mostly dead surface that PR6a will discover is wrong.
- **Fix:** Pick one of (a) drop the DI factory entirely and have PR6a use `lavalink_glue.get_lavalink_client()` (returns `Optional[Client]`) — that's the pattern `/lltest` already uses and it composes cleanly with the "service is down" branch; the DI registration is one of those premature flexibilities that PR6a will pay for. Or (b) keep the factory but **document explicitly** in the docstring that callers must catch `DependencyNotSatisfiableException` and inspect `__cause__`, and add a test pinning that contract so PR6a doesn't get blindsided. (a) is closer to KISS and keeps `/play` honest about the actual lifecycle. The factory + `LavalinkNotReadyError` pair as it stands is two layers of indirection for a bool check.

### MEDIUM-2 — Voice-state listener gates the handshake-ready signal behind `_ll_client is None`

- **Severity:** MEDIUM
- **Where:** `src/ryzic/lavalink_glue.py:358-374`
- **Why it matters:** `_on_voice_state_update` returns early if `_ll_client is None`. The `_voice_ready_event(guild_id).set()` block lives **after** that early return. Practical implication: if a `VoiceStateUpdateEvent` for our own user arrives during the bootstrap window — e.g., a residual state from a previous session that hikari delivers right after `ShardReadyEvent` — the event will not be marked ready. In normal `/play` flow this is benign (PR6a's `/play` requires the lavalink client up before it gets to the handshake-wait), but the docstring on `_on_voice_state_update` doesn't reflect that the handshake-ready bookkeeping shares fate with lavalink dispatch. It's an invisible coupling: someone reading "voice handshake race fix" elsewhere in the codebase will assume the event is set on every bot-self join, and the bug will only surface under reconnect timing.
- **Fix:** Move the handshake-ready bookkeeping above the `_ll_client is None` short-circuit (or split into two separate listeners — bridge-forwarder and handshake-tracker — so each has a single responsibility per SRP). Two listener functions wired separately is the cleaner shape: it also makes `_on_voice_server_update` / `_on_voice_state_update` / `_on_voice_state_handshake` independently testable.

### MEDIUM-3 — `_on_voice_state_update` short-circuit is not tested

- **Severity:** MEDIUM
- **Where:** `tests/test_lavalink_bridge.py` — only `test_voice_server_listener_short_circuits_when_client_missing` exists; the symmetric state-listener test is missing.
- **Why it matters:** Both listeners contain the same `if _ll_client is None: return` guard. Coverage of one but not the other is the kind of asymmetry that lets a future refactor regress one half silently. The PR description's test plan claims "listener short-circuit when client not yet bootstrapped" without qualifier — it's only half implemented.
- **Fix:** Add a parallel `test_voice_state_listener_short_circuits_when_client_missing` mirroring the server one. Five lines.

### LOW-1 — `_make_starter` factory adds indirection without earning it

- **Severity:** LOW
- **Where:** `src/ryzic/bot.py:39-48, 69`
- **Why it matters:** The implementer's report explains the indirection as "lambda fails inspect.iscoroutinefunction check". That's true for lambdas, but `_make_starter` returns an `async def` regular function — `bot.subscribe(StartingEvent, async_def_handler)` accepts it directly (verified in `.venv/lib/python3.13/site-packages/hikari/impl/event_manager_base.py:441-449`, which calls `inspect.iscoroutinefunction(callback)`). The factory closure exists only to capture `client` — a top-level `async def _on_starting(event, client=client)` or, more simply, a top-level coroutine that takes `client` from a module-level closure / partial would be the same shape with less ceremony. Net effect: 10 lines + a `Callable[[hikari.StartingEvent], Coroutine[None, None, None]]` type signature + `TYPE_CHECKING` import for what could be five lines inline:
  ```python
  async def _on_starting(_: hikari.StartingEvent) -> None:
      await client.load_extensions("ryzic.commands.lltest")
      await client.start()
  bot.subscribe(hikari.StartingEvent, _on_starting)
  ```
  defined inside `main()` after `client` is created. Captures `client` via lexical scope. The factory pattern is a leftover from a class-based design that didn't survive simplification.
- **Fix:** Inline the closure into `main()`. Drop `_make_starter`, drop `Callable`/`Coroutine` from `TYPE_CHECKING`. Reads better, no behavior change.

### LOW-2 — `_auto_leave` swallows `CancelledError` instead of re-raising

- **Severity:** LOW
- **Where:** `src/ryzic/lavalink_glue.py:120-124`
- **Why it matters:** Standard asyncio idiom is to re-raise `CancelledError` after cleanup so the task is marked `cancelled()` rather than completed. Returning normally from the cancel handler means `task.cancelled()` returns False after cancellation — and indeed the test at `tests/test_lavalink_bridge.py:282` had to weaken the assertion to `first_task.cancelled() or first_task.cancelling()` to cope. The `cancelling()` half passes for the wrong reason (it's truthy because the cancellation was *requested*, not because the task acknowledged it). Functionally harmless here because no caller waits on the task to settle, but it's a smell that makes the test less informative and complicates future refactors that might `gather()` these tasks.
- **Fix:**
  ```python
  async def _auto_leave(bot: hikari.GatewayBot, guild_id: int) -> None:
      try:
          await asyncio.sleep(AUTO_LEAVE_SECONDS)
      except asyncio.CancelledError:
          raise
      ...
  ```
  Or just drop the `try/except` entirely — `asyncio.sleep` re-raises by default; the explicit handler is doing nothing useful. Tighten `test_auto_leave_replaces_existing_timer` to assert `first_task.cancelled()` (await it briefly first to let the cancellation land).

### LOW-3 — `wait` happens-before-`set` ordering is not tested

- **Severity:** LOW
- **Where:** `tests/test_lavalink_bridge.py:196-237`
- **Why it matters:** The handshake-race fix is the load-bearing piece of the PR. Existing tests cover (a) join-then-wait (event already set), (b) wait-then-timeout (no join). The interesting case for a race fix is **wait first, then join sets event** — that's what `/play` will actually do (call `bot.update_voice_state` then `await wait_for_voice_ready`). The current tests don't pin that ordering. A regression where `_voice_ready_event` is created twice (e.g., a future change replaces the dict lookup with `_voice_ready_events.setdefault(guild_id, asyncio.Event())` called at both sides racing) would not be caught.
- **Fix:** Add a test along the lines of:
  ```python
  async def test_wait_for_voice_ready_resolves_when_join_arrives_after_wait_starts() -> None:
      lavalink_glue._set_lavalink_client_for_test(...)
      wait_task = asyncio.create_task(lavalink_glue.wait_for_voice_ready(111, timeout=1.0))
      await asyncio.sleep(0)  # let wait start
      await lavalink_glue._on_voice_state_update(_make_voice_state_event(user_id=42, bot_user_id=42, channel_id=999))
      assert await wait_task is True
  ```

### LOW-4 — `voice_update_handler` is awaited from listener body; long lavalink ops will block listener throughput

- **Severity:** LOW
- **Where:** `src/ryzic/lavalink_glue.py:352-361`
- **Why it matters:** hikari dispatches listeners as `asyncio.create_task` per-listener (so multiple listeners run concurrently for the same event), but inside a single listener the body runs sequentially. `voice_update_handler` issues a REST call to lavalink (`update_player`), which blocks the listener until that round-trip completes. In practice it's fast; in a degraded-network scenario with the lavalink server unreachable, every voice event for every guild blocks until the per-request timeout. lavalink.py doesn't expose `update_player` as fire-and-forget. Not a bug — noting because the implementer's claim "listeners short-circuit when client not yet bootstrapped" doesn't extend to "and never block on a slow lavalink." Future-incident watchlist.
- **Fix:** None required for M1. Document in the watchlist.

### LOW-5 — `EventHandler.on_track_exception` formats `event.cause` (a stack trace) into the user-facing notice

- **Severity:** LOW
- **Where:** `src/ryzic/lavalink_glue.py:227`
- **Why it matters:** `event.message` is `Optional[str]` (a short cause description). `event.cause` is the JVM stack-trace string from Lavalink — multi-line, often hundreds of chars. The fallback `event.message or event.cause` will dump a stack trace into the channel when `message` is null (which lavalink does emit for some failures). Discord will still send it but it's UX noise. PR6a will likely move this to an embed builder; the inline version is for the throwaway window so it's low-impact.
- **Fix:** Cap to first line or first 120 chars: `(event.message or event.cause or "unknown").splitlines()[0][:120]`. Or just `event.message or "unknown error"` — `cause` is debugger fodder, not user-facing.

### LOW-6 — `/lltest` calls `player_manager.create()` purely as a side-effecting reachability probe

- **Severity:** LOW
- **Where:** `src/ryzic/commands/lltest.py:48-52`
- **Why it matters:** `create()` instantiates a `DefaultPlayer` and stores it in `player_manager.players[guild_id]`. The lltest never uses the returned player; it just wants to verify that node selection works. A user who runs `/lltest` then `/play` (when PR6a lands) will hit the existing player. That's idempotent (`create` returns the existing player), so no functional bug — but it's a mismatch between the command's apparent purpose ("smoke check") and its actual side effect ("create a player on the lowest-penalty node"). Keep in mind for PR6b's removal — `/lltest` going away means orphaned players go away with it.
- **Fix:** Either drop the `create()` call (just listing nodes is enough for "lavalink reachable"), or add a comment that this intentionally exercises the node-selection path PR6a will rely on. The latter is more useful for the wire-up validation goal but should be explicit.

### LOW-7 — `_clear_player_queue` casts `BasePlayer` access via getattr; the cast in `on_track_stuck` does not

- **Severity:** LOW
- **Where:** `src/ryzic/lavalink_glue.py:144-152` vs `src/ryzic/lavalink_glue.py:245`
- **Why it matters:** Two places handle the BasePlayer-vs-DefaultPlayer split inconsistently. `_clear_player_queue` uses `getattr(player, "queue", None)` for runtime safety. `on_track_stuck` does `cast(lavalink.DefaultPlayer, event.player).skip()` — type-only, no runtime guard. If the codebase ever uses a custom player subclass that doesn't implement `skip()`, the cast hides the bug; if it skips `queue`, the getattr handles it gracefully. Pick one convention. Plan §7 commits to DefaultPlayer; both could just type-assert it once.
- **Fix:** Either (a) cast at the boundary (`event.player`) once and treat the typed local as DefaultPlayer everywhere, or (b) keep the runtime guards consistently. The hybrid is the maintenance hazard.

### LOW-8 — `auto_leave_tasks.pop(guild_id, None)` self-pop has a small race with rescheduling

- **Severity:** LOW
- **Where:** `src/ryzic/lavalink_glue.py:126`
- **Why it matters:** When `_auto_leave` finishes its sleep and reaches line 126 to pop itself from the dict, a task switch could let `_start_auto_leave(guild_id)` run between sleep-completion and pop. The new sequence: `_cancel_auto_leave` pops the (about-to-finish) old task → cancels it (already past the cancel point, so no-op) → `_start_auto_leave` inserts NEW task → old `_auto_leave` resumes and pops the NEW task from the dict. Result: a live auto-leave task with no entry in `auto_leave_tasks`, so the next `_cancel_auto_leave` is a no-op and the timer fires when it shouldn't. The window is microseconds wide and requires a `QueueEndEvent` to land in that exact window, which itself requires the queue to have just emptied. Realistically reachable only under stress/CI. Worth a note in the watchlist.
- **Fix:** Drop the self-pop and use `discard`-style: `if auto_leave_tasks.get(guild_id) is asyncio.current_task(): del auto_leave_tasks[guild_id]`. Or simpler: don't track completion at all; let the dict carry the completed task until the next cancel/replace clears it (Python tasks don't leak memory in this shape).

### LOW-9 — Module-level `_voice_ready_events` cross-test contamination is mitigated only by the `autouse` fixture

- **Severity:** LOW
- **Where:** `tests/test_lavalink_bridge.py:108-111` (the `_reset_state` fixture)
- **Why it matters:** Module-level state per plan §8 is a deliberate KISS choice and the autouse fixture handles it. Just noting: any future test file that uses `lavalink_glue` and forgets the autouse fixture will see leaked state. The reset helpers (`_reset_state_for_test`, `_set_lavalink_client_for_test`) are named clearly enough that this is unlikely to bite, but worth documenting in `lavalink_glue.py`'s "test seams" section that downstream tests must call them.
- **Fix:** Move the fixture into `tests/conftest.py` so it autouses across the entire test directory, or add a one-line note in the test-seams docstring directing readers to the fixture pattern.

### NOTE — Comment hygiene is good

- **Severity:** observation, not a finding
- **Where:** `src/ryzic/lavalink_glue.py` throughout
- The module docstring and inline comments document **why** (the #153 mitigation, the handshake race, why `removeprefix` over `[6:]`) rather than narrating **what**. Two TODOs (PR6a wiring) explain the deferred forward references. The `_clear_player_queue` docstring explains the BasePlayer vs DefaultPlayer split, which is exactly the kind of non-obvious context worth recording. Aligns with the maintainer's standards.

### NOTE — Future-incident watchlist

- **Long-blocking listener under degraded lavalink:** see LOW-4. If the server stops responding, every voice event blocks per request timeout. Probably fine; flag if future incidents tie back to voice-event throughput.
- **Auto-leave self-pop race:** see LOW-8. Only matters under churn.
- **Channel-switch handshake:** `wait_for_voice_ready` returns True immediately after the first join; if the bot ever switches channels mid-session (currently disallowed by the plan, but a future "follow VC" feature might), the second handshake would race because the event is already set. Reset on `update_voice_state(guild_id, new_channel)` would be needed.
- **Singleton DI lifetime:** linkd caches singletons at first successful resolve. If `_ll_client` is ever swapped (e.g., PR-future "rebuild on persistent failure"), the DI cached instance will be stale. Currently moot — PR keeps the client across reconnects deliberately.
- **`/lltest` orphaned player:** see LOW-6. PR6b removes `/lltest`; clean up players-from-lltest as part of that PR or document that `/leave` resets state.

---

## Verdict

**minor revisions.**

The plan §7 hazards are all addressed correctly: `removeprefix("wss://")` not `[6:]`, both listeners short-circuit when `_ll_client is None`, EventHandler covers all required hooks, the voice-handshake race fix is implemented and timeout-bounded, the 5-minute auto-leave is cancellable on `TrackStartEvent` and replaceable on subsequent `QueueEndEvent`, `TrackEndEvent` never calls `player.play()` (#153 mitigation), `TrackStuckEvent` skips with notification (#144 mitigation), `NodeDisconnectedEvent` notifies last-known channels and clears queues. State model matches plan §8 (three module-level dicts, no `GuildState`). Conventional-commit message, scope is tight, comments narrate why not what, ruff/ty/pytest all green.

The blocking-ish issue is **MEDIUM-1** (the DI factory raising a custom error that linkd will wrap, breaking the contract PR6a is supposed to consume). Recommend dropping the DI factory in favor of the `get_lavalink_client()` pattern that `/lltest` already uses, and removing `LavalinkNotReadyError` along with it. **MEDIUM-2** and **MEDIUM-3** are quick mechanical fixes (move two lines + add one test). The LOWs are nice-to-haves; LOW-1 (drop `_make_starter`) and LOW-2 (re-raise `CancelledError`) are worth doing for code-hygiene; the rest can wait or land as an in-PR cleanup commit.

Once the MEDIUMs are addressed, ship.
