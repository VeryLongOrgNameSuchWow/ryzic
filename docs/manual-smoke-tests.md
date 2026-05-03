# Manual smoke tests

End-to-end verification run against a real Discord server + a real voice channel before each release. Automated tests cover units and a Lavalink container; this checklist covers everything that requires a live Discord gateway and human ears.

Prerequisites:

- A test Discord server you own.
- A clean clone with `.env` populated per [README.md](../README.md).
- Two voice channels in the test server, plus one stage channel (for stage rejection — requires Server Settings → Enable Community).
- A second Discord account (or a friend) is helpful but not required.

Reset state between runs with `docker compose down -v && docker compose up -d` if you want a cold cache.

## Bring-up

- [ ] `docker compose up -d` brings both services up; `docker compose ps` shows `lavalink` as healthy and `ryzic` as running within ~60s.
- [ ] `docker compose logs ryzic` shows `Logged in as <bot> (<id>)` and a node-connected line.
- [ ] `/ping` responds (sanity check that slash commands registered).

## Core playback

- [ ] `/play <youtube_track_url>` (e.g. `https://www.youtube.com/watch?v=dQw4w9WgXcQ`) plays audio in your voice channel within ~3s.
- [ ] `/play <youtube_playlist_url>` queues every track in the playlist; the embed reports the correct count and total duration.
- [ ] `/queue` shows the currently-playing track with `mm:ss / mm:ss` progress and the next entries (paged at 10).
- [ ] `/skip` advances to the next track immediately; the embed names the skipped title.
- [ ] `/pause` halts playback; `/resume` resumes from the same position (no audible jump).
- [ ] `/leave` disconnects the bot and clears the queue; `/queue` afterwards reports empty.

## Auto-leave

- [ ] After the final track in a queue ends, the bot stays connected briefly, then disconnects after ~5 minutes of idle and posts `Idle for 5 minutes — disconnecting.` in the channel where `/play` was last used.
- [ ] `/play` during the idle window cancels the timer; the bot does not disconnect.
- [ ] (Optional) Set `RYZIC_AUTOLEAVE_SECONDS=0` in `.env`, `docker compose up -d`, queue then drain a track. The bot stays connected indefinitely (24/7 ambient mode). **Restore `RYZIC_AUTOLEAVE_SECONDS` afterwards.**

## Cache behavior

- [ ] `/play` the same video twice in a row: the second play does **not** re-download (visible in `docker compose logs ryzic` as a cache-hit log; no yt-dlp activity).
- [ ] Two `/play` calls for the same URL within ~1s yield exactly one yt-dlp download (verify in `docker compose logs ryzic` — only one "downloading" log line per video). Both interactions report success.
- [ ] Cache survives `docker compose restart ryzic`: re-`/play` the same video after restart still hits cache (no yt-dlp re-download). The queue itself is empty post-restart (per-guild state is in-memory by design).
- [ ] LRU eviction triggers at the configured size: set `RYZIC_CACHE_MAX_GB=1` in `.env`, `docker compose up -d`, queue several long tracks (live concerts work), and confirm via `du -sh` on the cache volume that the directory stays under the cap and the oldest tracks are gone. **Restore `RYZIC_CACHE_MAX_GB` to your previous value afterwards.**
- [ ] The currently-playing file is **never** evicted even when the cap is exceeded (queue a long track on a tight cap and confirm playback continues without error).

## Error handling

- [ ] `/play https://youtube.com.invalid/watch?v=x` is rejected by the URL validator with `Only YouTube URLs are supported.`
- [ ] `/play <livestream_url>` is rejected with the livestream-not-supported message before any download starts.
- [ ] `/play <private_video_url>` surfaces `That video is private.`
- [ ] `/play <age_restricted_url>` surfaces `That video is age-restricted and can't be played.`
- [ ] Simulated yt-dlp breakage: run `docker compose exec ryzic python -m pip install 'yt-dlp==2021.1.15'` against a hot container (a real-but-stale release that fails against current YouTube), then `/play <track_with_cached_metadata>` for a track NOT in audio cache, and `/play <playlist>` for a playlist with cached metadata. Both should yield friendly errors and the cached-playlist warning footer respectively. **Restore with `docker compose down && docker compose up -d --force-recreate`** — `restart` does **not** undo `pip install`, the broken package persists in the container layer.

## Discord-side rejections

- [ ] `/play` from a stage channel is rejected with `Stage channels aren't supported. Use a regular voice channel.`
- [ ] `/play` from a DM never reaches the bot (Discord hides the command).
- [ ] `/skip`, `/pause`, `/resume`, `/leave` from a user not in the bot's voice channel return the `Join <#channel>` ephemeral.

Coverage and unit-test acceptance criteria (plan §11.9 / §11.10) are enforced in CI, not here.

## Sign-off

- [ ] All boxes ticked.
- [ ] Release version: `___________`
- [ ] Tester: `___________`
- [ ] Date: `___________`
