# PR #3 — Security Review

**PR**: `feat(audio): lavalink.py wire-up + voice bridge`
**Branch**: `feat/lavalink-wireup` → `main`
**Repo**: `VeryLongOrgNameSuchWow/ryzic`
**Commit reviewed**: `de57af4`
**Files in scope** (PR diff only):

- `src/ryzic/lavalink_glue.py` (new, 456 lines)
- `src/ryzic/commands/lltest.py` (new, 60 lines)
- `src/ryzic/commands/__init__.py` (new, 1 line)
- `src/ryzic/bot.py` (+19/-2)
- `tests/test_lavalink_bridge.py` (new, 299 lines)

No dependency or build-system changes. `hikari`, `lavalink`, `hikari-lightbulb` were already declared in PR #1.

This review covers the ten focus areas from the brief: voice-update bridge pass-through, scheme-stripping safety, DI factory credentials, singleton init race, `LavalinkNotReadyError` leakage, `/lltest` info disclosure, `WebSocketClosedEvent` channel-routing trust, `TrackException` payload leakage, auto-leave timer hygiene, and test isolation.

---

## Findings

### 1. Voice-update bridge — pass-through, no logging of secrets

**Severity**: (informational — no finding)
**What**: `_bridge_voice_server_payload` and `_bridge_voice_state_payload` (`src/ryzic/lavalink_glue.py:312-349`) read `endpoint`/`token`/`session_id` straight off the hikari event into a dict and hand it to `lavalink.Client.voice_update_handler`. No logging at any level touches `event.token` or `state.session_id`. Grepped every `_log.*` call in `lavalink_glue.py`: the only voice-shape values that ever reach a log line are `guild_id`, `code`, `reason`, `node.name`, and `cfg.lavalink_host`/`cfg.lavalink_port` — never `token`, `session_id`, or `password`. The lavalink.py library itself (verified `transport.py:151`/`transport.py:401`) sends the password as `Authorization` header but does not log it; its `_log.info` lines reference `node.name` only.

**Endpoint trust model**: the endpoint string is delivered over Discord's authenticated TLS gateway after the bot's token has authenticated the session. ryzic forwards it opaquely to lavalink.py, which forwards it in an `update_player` REST call to the lavalink server, which then opens its own WSS connection. Discord controls the value end-to-end; if Discord's gateway is itself compromised, the threat model has bigger problems than ryzic's pass-through. **Verdict: clean.**

---

### 2. `removeprefix("wss://")` — scheme handling

**Severity**: (informational — no finding)
**What**: `event.endpoint` (the hikari property, `voice_events.py:128-138`) returns `f"wss://{self.raw_endpoint}"` when `raw_endpoint` is non-None, else `None`. The bridge calls `endpoint.removeprefix("wss://") if endpoint else None`. If hikari ever changes the prefix to `ws://` or omits scheme entirely:

- `ws://host` → `removeprefix` no-op → lavalink receives `ws://host` (mostly harmless: `nodemanager.get_region` does a `startswith` substring scan against region prefixes — `ws://` is not a region prefix, so the lookup returns `None`, and the player is assigned to the configured `"us"` node anyway). The endpoint string is then forwarded to the lavalink server which would fail to parse it as a hostname; effect is a connection failure for that guild, not a redirect.
- `host` (no scheme) → `removeprefix` no-op → lavalink receives bare host → works as today.
- `wss://attacker.example` (Discord platform-side bug or attacker-controlled gateway) → strip → `attacker.example` forwarded to lavalink server, which would open a WSS connection there for the voice traffic. **However**, this requires Discord itself to send a malicious endpoint over an authenticated session, which is outside the threat model. There is no allowlist of `*.discord.media` / `*.discord.gg` hosts as a defense-in-depth check — see #10 for the LOW.

The choice of `removeprefix` over `[6:]` is explicitly documented (lines 28-30) and pinned by `test_bridge_voice_server_payload_does_not_substring`. Strong. **Verdict: clean for the chosen threat model.**

---

### 3. DI factory and lavalink.Client construction — credentials handling

**Severity**: (informational — no finding)
**What**: `_build_lavalink_client` (lines 391-404) reads `cfg.lavalink_password` from a frozen-dataclass `Config` (`config.py:24`, `field(repr=False)` so the password never appears in `repr(cfg)`). It is passed positionally to `client.add_node(...)` and stored inside `Transport._password` (lavalink/transport.py:84) as a `Final[str]`. The only log line touching the connection at INFO (`lavalink_glue.py:403`) reads `host=%s port=%d` — no password. lavalink.py's own logs use `node.name` (set explicitly to `"ryzic-default"`, line 400, so default `f'{region}-{host}:{port}'` fallback is bypassed).

The DI factory `_lavalink_client_factory` (lines 425-428) returns the singleton or raises `LavalinkNotReadyError`. No credential touches the registry path. **Verdict: clean.**

---

### 4. Singleton `_ll_client` init — race under concurrent ShardReadyEvents

**Severity**: (informational — no finding)
**What**: `_on_shard_ready` (lines 377-388):

```python
async def _on_shard_ready(...) -> None:
    global _ll_client
    if _ll_client is not None:
        return
    _ll_client = _build_lavalink_client(bot, cfg, event.my_user.id)
```

There is **no `await` between the check and the assignment**. `_build_lavalink_client` is fully synchronous (`lavalink.Client.__init__`, `add_node`, `add_event_hooks` are all sync — verified in `client.py:105-124`, `nodemanager.py:87-126`, `client.py:201`). hikari dispatches each subscriber as its own task (`event_manager_base.py:564`), so two concurrent `ShardReadyEvent`s would create two tasks, but each runs the body atomically with respect to the other under asyncio's single-threaded scheduling. The second task sees `_ll_client is not None` and returns. **No race possible.**

**Side note**: even though `lavalink.Client.__init__` constructs `aiohttp.ClientSession` (`client.py:118`), aiohttp only requires a running loop at request time, not at construction — and we are inside an async event handler so a loop is running anyway. **Verdict: clean.**

---

### 5. `LavalinkNotReadyError` — credential leakage in message

**Severity**: (informational — no finding)
**What**: `_lavalink_client_factory` raises `LavalinkNotReadyError("Lavalink client requested before ShardReadyEvent fired.")` (line 427). String is hardcoded, contains no host/port/password/state. The exception class itself extends `RuntimeError` with no extra fields. Even if a slash command catches this and echoes it to the user via lightbulb's default error reporter, the user only sees the hardcoded message. **Verdict: clean.**

---

### 6. `/lltest` — surface area and information disclosure

**Severity**: LOW (defense-in-depth + side effect)
**What**: `LLTest` (`commands/lltest.py:19-60`) has no `default_member_permissions`, no `dm_enabled=False`, and no admin gate. Any guild member with the application-default "Use Application Commands" permission can invoke it. The response is `ephemeral=True` so visible only to the invoker.

**What it exposes** (per node, all ephemeral): `node.name` ("ryzic-default" — admin-controlled, set in `_build_lavalink_client`), `node.region` ("us"), `node.available` (bool). No host, port, password, or session id. As long as a future maintainer doesn't drop the explicit `name="ryzic-default"` (which would let lavalink default `name` to `f'{region}-{host}:{port}'`, leaking host:port in the response), the information surface is a static admin-chosen string. The `_log.info` line at `lavalink_glue.py:403` already logs `host=%s port=%d` once at startup, so the values are not secret per se — but the pattern of "node.name is safe to surface" depends on the explicit override.

**Side effect**: line 49, `ll_client.player_manager.create(guild_id=ctx.guild_id)`. This **creates a player** inside lavalink.py's PlayerManager (`playermanager.py:170-260`). The call is idempotent per guild (`if guild_id in self.players: return self.players[guild_id]`), so spam from one guild creates one player. Across guilds, the cap is the number of guilds the bot is in — bounded, not exhaustible. But a smoke-check command that mutates state is surprising; the M1 plan calls this command "throwaway" and deletes it in PR6b once `/play` exists.

**Where**: `src/ryzic/commands/lltest.py:19-60`.
**Why it matters**: Mostly a hygiene call. The command is documented as throwaway. Two concrete deltas worth making before merge:

1. Add a comment next to `name="ryzic-default"` in `_build_lavalink_client` warning that the name leaks to `/lltest` invokers if the explicit override is dropped.
2. Consider gating `/lltest` to `default_member_permissions=hikari.Permissions.MANAGE_GUILD` for its short remaining lifetime — minimal cost, removes the "any user can mutate PlayerManager state" property.

Neither blocks merge.

---

### 7. `last_play_channel` — channel-routing trust

**Severity**: LOW (channel cleanup, not exploitable)
**What**: `last_play_channel: dict[int, int]` (line 54) maps guild_id → channel_id. In this PR, **only `_reset_state_for_test` writes to it**; PR6a's `/play` will populate it. The brief asks: "could a malicious user manipulate `last_play_channel` to make the bot post in a different channel?"

Threat-model walk: the only writer (in this PR's design and per M1 §8) is `/play`, which writes `ctx.channel_id` of its own invocation. That means the entry reflects the **last user who invoked `/play` in that guild** — which is by design where error messages should land. A user CAN cause the bot to post in a channel of their choosing by invoking `/play` there; there is no cross-guild leak (keyed by guild_id) and no way to direct posts to a channel the user doesn't already have access to (Discord's slash-command surface only exposes channels the user can see).

Remaining residual: `_send_to_last_play_channel` (lines 155-166) catches `hikari.HikariError` (channel deleted, bot lacks Send Messages permission), logs at WARNING, and moves on. The dict entry is **not removed** on failure — a deleted-channel id will sit in the dict forever, generating one warning per future failure. Bounded by # of guilds. Not security; a LOW hygiene nit.

**Where**: `src/ryzic/lavalink_glue.py:155-166`.
**Fix** (optional): on `hikari.NotFoundError` specifically, `last_play_channel.pop(guild_id, None)` so the stale entry is forgotten.

---

### 8. `TrackExceptionEvent` — server-side error text posted publicly to channel

**Severity**: MEDIUM (information disclosure + Discord markdown injection)
**What**: `on_track_exception` (lines 210-228) posts:

```python
f"Track **{title}** failed: {event.message or event.cause}. Skipping."
```

via `_send_to_last_play_channel`, which calls `bot.rest.create_message(channel_id, content)`. This message is **public to the channel** (not ephemeral; `create_message` has no ephemeral flag — that's a slash-interaction concept).

Two distinct concerns:

(a) **`event.cause` is a Java exception string from the lavalink server** (`events.py:121-136`: `cause: str` is the cause-of-exception text; `cause_stacktrace: str` is the full stacktrace, which we correctly do NOT use). Real-world `cause` values for the YouTube source plugin can include:
  - Internal lavalink server filesystem paths (`/opt/Lavalink/...`, especially in sideloader/plugin error paths).
  - Java exception class FQNs and the failing URL (which itself may contain query-string secrets if a yt-dlp-resolved track URL embedded a signed `pot` token or session-bound stream-id).
  - Hostnames / IPs of internal infrastructure if the lavalink server is on a private network.

These leak to **every user in the channel**, not just to the `/play` invoker.

(b) **No Discord markdown sanitisation** on `title` or `event.message`/`event.cause`. The `**…**` wrapper around `title` makes a YouTube-supplied title actively render as markdown — a video titled `*x*\n# pwn` would break formatting. M1 §6 item 10 explicitly mandates "strip backticks from yt-dlp error strings BEFORE wrapping in inline code", and item 11 mandates length truncation via `safe_truncate`. Neither helper exists yet (`ux.py` is a future PR), so the implementer correctly couldn't call them — but this handler ships TODAY and posts unsanitised text TODAY (in any environment where this PR is merged before PR6's `ux.py`).

  - `@everyone` injection in titles is **already blocked**: hikari's `bot.rest.create_message` always emits `allowed_mentions: {parse: []}` by default (verified in `hikari/internal/mentions.py:67-90` and `hikari/impl/rest.py:1549-1552`). So track titles cannot trigger mass-mentions.
  - Length: Discord rejects messages > 2000 chars with HTTP 400, which `_send_to_last_play_channel` swallows as `hikari.HikariError`. Long Java causes simply produce a warning log instead of posting — UX issue, not security.

**Where**: `src/ryzic/lavalink_glue.py:224-228`.
**Why it matters**: This is the same scrubbing rule the M1 plan called out for the yt-dlp wrapper (PR2 review item 10), now applied at the lavalink boundary. A self-hoster's lavalink server may run with default plugins that surface internal paths in exception text. Posting that to a public channel violates the "internal info stays internal" property the plan implicitly assumes.

**Fix**: replace the message with a sanitised, capped version. Until `ux.py` lands, do it inline:

```python
detail = (event.message or event.cause or "unknown error")
# Strip backticks/asterisks/underscores so the snippet can't break out of
# inline code formatting; cap so a Java stack trace can't flood the channel.
detail = re.sub(r"[`*_~|]", "", detail).splitlines()[0][:200]
title_safe = re.sub(r"[`*_~|]", "", title)[:100]
await _send_to_last_play_channel(
    self._bot, guild_id,
    f"Track **{title_safe}** failed: `{detail}`. Skipping.",
)
```

(Alternatively: post a fixed message `"A track failed and was skipped."` and only log the `event.cause`/`event.message` server-side. Loses some UX, gains "no untrusted content ever leaves the server.")

The same fix should be applied to the `TrackStuckEvent` handler (line 248-252) — title-only there, so a single regex cap on `title` suffices. The `WebSocketClosedEvent` (line 277-279) and `NodeDisconnectedEvent` (line 301-305) handlers post hardcoded strings, no sanitisation needed.

---

### 9. Auto-leave timer — task lifecycle and growth

**Severity**: LOW (hygiene, not exploitable)
**What**: `_start_auto_leave` (lines 108-117) replaces any existing timer for the same guild via `_cancel_auto_leave`, so the dict size is bounded by the number of guilds the bot is in. No unbounded growth.

The cleanup paths are:

- `_cancel_auto_leave` is called from `TrackStartEvent` (line 184) and `WebSocketClosedEvent` 4014 (line 275). These are the routine cancellation paths.
- `_auto_leave` itself does `auto_leave_tasks.pop(guild_id, None)` after the sleep (line 126), so the entry is cleaned up after firing.
- `bot.update_voice_state(guild_id, None)` failures are caught with a broad `except Exception` (line 137-138). This is fine.

**Gap**: if the bot is **kicked from a guild** while the timer is mid-sleep, the task continues to completion 5 minutes later, then tries `update_voice_state` (will fail, swallowed) and `_send_to_last_play_channel` (will fail, swallowed). No crash, but two wasted REST attempts. There is no `GuildLeaveEvent` listener to proactively cancel the task. M1 §8's restart behaviour ("All state dies on restart") tolerates this, and the per-guild ceiling means the wasted work is bounded by guilds-being-kicked-during-idle.

**Where**: `src/ryzic/lavalink_glue.py:108-141`.
**Fix** (optional): subscribe to `hikari.GuildLeaveEvent` and call `_cancel_auto_leave(event.guild_id) + last_play_channel.pop(event.guild_id, None) + _voice_ready_events.pop(event.guild_id, None) + _ll_client.player_manager.destroy(...)` to clean up the per-guild state. Outside this PR's brief; arguably belongs to PR6.

**Test hygiene side note** (LOW, separate from production code): `test_auto_leave_replaces_existing_timer` (tests/test_lavalink_bridge.py:273-282) calls `_start_auto_leave` twice. The autouse `_reset_state` fixture (lines 108-111) clears the dict but does **NOT cancel the second pending 300s sleep task**. The task is leaked into the asyncio event loop until pytest-asyncio tears down the loop at the end of the function-scoped test. pytest-asyncio's default loop scope is function, so the leak is recovered after each test, but the `_reset_state` fixture should call `_cancel_auto_leave(guild_id)` for any pending timer to be tidy. Tests pass in 0.40s with no warnings; not a correctness issue, just untidy.

---

### 10. Endpoint allowlist — defense-in-depth (none today)

**Severity**: LOW (defense-in-depth gap)
**What**: The endpoint forwarded to lavalink is `event.raw_endpoint` (Discord-supplied) with `wss://` stripped. No regex check, no allowlist of known Discord voice-server suffixes (`*.discord.media`, `*.discord.gg`). M1 §6 doesn't require one; the security model trusts Discord's authenticated gateway as the source of truth.

**Where**: `src/ryzic/lavalink_glue.py:312-331`.
**Why it matters**: If Discord's gateway were ever to send a malicious endpoint (gateway compromise, bot-token-replay attack against a stale connection, etc.), lavalink would obey and connect there. An allowlist (`endpoint.endswith((".discord.media", ".discord.gg"))`) would be a free defense-in-depth check. Not blocking; not in scope of M1.

**Fix** (optional, deferable to a hardening PR): add a `_VALID_ENDPOINT_SUFFIXES = (".discord.media", ".discord.gg")` constant and reject (return without forwarding, log at WARNING) any endpoint that doesn't match.

---

### 11. Tests do not hit any real network

**Severity**: (informational — no finding)
**What**: `test_lavalink_bridge.py` constructs hikari events directly (`hikari.VoiceServerUpdateEvent(app=cast(Any, _FakeApp()), ...)`, `hikari.VoiceState(...)`) — no gateway connection. The bridge listeners are fed a `_FakeLavalinkClient` (lines 98-105) that only appends to a list. `_FakeApp` (lines 31-50) is a duck-typed stub for `hikari.GatewayBot` whose `rest.create_message` and `update_voice_state` methods append to in-memory lists.

Confirmed by running the suite: 20 tests pass in 0.40 s with no network warnings. No `aiohttp.ClientSession`, `discord.py`, `socket`, or `urllib.request` import in `tests/`. **Verdict: clean.**

---

## Summary

| Severity | Count |
| --- | ---: |
| HIGH (merge-blocking) | 0 |
| MEDIUM (fix soon) | 1 |
| LOW (nit) | 4 |

The single MEDIUM is **#8 (TrackException posts unsanitised server-side error text to a public channel)** — a 4-line inline fix until `ux.py` lands, or a 1-line "fixed message" alternative. This is the only finding with a real exploitability story (lavalink server exposes internal paths/hostnames in exception text → public Discord channel). The four LOWs are defense-in-depth (#6, #7, #9, #10).

The brief's primary attack vectors — credential leakage in logs/messages/exceptions, scheme-strip injection, singleton init race, `/lltest` info leak, `last_play_channel` channel-routing manipulation, auto-leave runaway — are all defended (the endpoint trust model is a stated assumption, not a defended boundary). 20 unit tests, all passing in 0.40 s, mocked-network only.

## Verdict

**fixes recommended** — not merge-blocking. Address #8 (MEDIUM) in this PR or as the next immediate commit on the branch (since the handler ships and runs the moment a track fails). #6, #7, #9, #10 are optional polish; #6 should be revisited when `/lltest` is removed in PR6b.
