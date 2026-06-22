# Fix #170: Intentional leaves misfire voice-lost broadcast

## Problem

The `voice_lost` channel broadcast fires on paths where the disconnect is **intentional**, not a network failure. Pressing the controller's Leave button, running `/leave`, or hitting the idle auto-leave timer currently emit the "voice was lost" sentence alongside the intentional-leave broadcast.

## Root Cause

All disconnects (intentional and unintentional) result in Discord closing the voice WebSocket with code 4014. The `on_websocket_closed` handler catches ALL 4014 events and broadcasts `voice_lost`, with no mechanism to distinguish intentional from unintentional disconnects.

### Call paths

1. **Leave button** → `_handle_leave` → `bot.update_voice_state(guild_id, None)` → 4014 → `voice_lost`
2. **`/leave` command** → `_handle_leave` → `bot.update_voice_state(guild_id, None)` → 4014 → `voice_lost`
3. **Auto-leave** → `_auto_leave` → `bot.update_voice_state(guild_id, None)` → 4014 → `voice_lost`

## Solution

Add a `_pending_intentional_disconnects: set[int]` to track guilds where disconnect was intentional. Check this set in `on_websocket_closed` and skip the `voice_lost` broadcast if present, then clear the entry.

## Implementation

### lavalink_glue.py

1. Add module-level state:
   ```python
   _pending_intentional_disconnects: set[int] = set()
   ```

2. Add helper functions:
   ```python
   def _mark_intentional_disconnect(guild_id: int) -> None:
       """Mark a guild's disconnect as intentional so on_websocket_closed skips voice_lost."""
       _pending_intentional_disconnects.add(guild_id)

   def _clear_intentional_disconnect(guild_id: int) -> None:
       """Clear an intentional disconnect marker (called after WebSocket close processed)."""
       _pending_intentional_disconnects.discard(guild_id)
   ```

3. Modify `on_websocket_closed` (line 455-464):
   ```python
   if event.code != 4014:
       return
   if guild_id in _pending_intentional_disconnects:
       _pending_intentional_disconnects.discard(guild_id)
       # Skip voice_lost broadcast — disconnect was intentional
       # Still need to clear queue and reset state
       await clear_queue_releasing(cast(lavalink.DefaultPlayer, event.player))
       _cancel_auto_leave(guild_id)
       _reset_voice_ready(guild_id)
       # NOTE: no voice_lost broadcast, no teardown (leave handlers already did this)
       return
   # Unintentional disconnect — broadcast voice_lost
   await clear_queue_releasing(...)
   ...
   ```

4. Modify `_auto_leave` (line 177-178):
   - Call `_mark_intentional_disconnect(guild_id)` before `update_voice_state`
   - Note: `_auto_leave` broadcasts its own message before the disconnect, so we don't need to broadcast again

5. Export `_mark_intentional_disconnect` for use by `/leave` command.

### commands/leave.py

Modify `_handle_leave` (line 59):
- Import `_mark_intentional_disconnect` from `lavalink_glue`
- Call `_mark_intentional_disconnect(guild_id)` before `bot.update_voice_state(guild_id, None)`

### now_playing_buttons.py

No changes needed — it calls `_handle_leave` from `leave.py`, which will now mark the disconnect as intentional.

## Testing

1. Unit tests in `tests/test_lavalink_bridge.py`:
   - Add test for intentional disconnect skipping `voice_lost`
   - Add test for unintentional disconnect still broadcasting `voice_lost`
   - Add test for `_mark_intentional_disconnect` / `_clear_intentional_disconnect` lifecycle

2. Manual testing:
   - Click Leave button → should see only "Left voice channel", no "Voice connection lost"
   - Run `/leave` → same
   - Wait for auto-leave → should see only idle message, no "Voice connection lost"

## Files Changed

- `src/ryzic/lavalink_glue.py` — state tracking + `on_websocket_closed` logic
- `src/ryzic/commands/leave.py` — mark intentional disconnect
- `tests/test_lavalink_bridge.py` — new tests