# Security

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Report privately via [GitHub Security Advisories](https://github.com/VeryLongOrgNameSuchWow/ryzic/security/advisories/new). That's the preferred channel — it's private to maintainers and gives us a paper trail to coordinate the fix and the disclosure.

If GitHub Security Advisories isn't available to you, email `riohno@tutamail.com`.

## Supported versions

`ryzic` is pre-1.0. Only the latest release on the `0.x` line receives security fixes. If stability matters more than feature velocity, pin to a specific patch version.

| Version | Supported |
| --- | --- |
| latest `0.x` | ✓ |
| earlier `0.x` | ✗ |

## Disclosure policy

Coordinated disclosure. We'll acknowledge the report, work on a fix, cut a release, and publish a notice through the GitHub Security Advisory.

This is a single-maintainer project — response time is best-effort.

## Self-hoster responsibilities

`ryzic` is designed for self-hosted deployment. Some security characteristics depend on you:

- The audio cache directory contains copyrighted material. Restrict filesystem access accordingly.
- `LAVALINK_PASSWORD` defaults to the upstream Lavalink default (`youshallnotpass`) but Lavalink is not exposed on the host network — it's only reachable from inside the docker compose network. Don't expose it.
- `DISCORD_BOT_TOKEN` is the keys to your bot. Rotate it via the Discord Developer Portal if you suspect exposure.
- Cookies are deliberately disabled in the yt-dlp wrapper (no `cookiefile`, no `cookiesfrombrowser`). Re-enabling them would expose your YouTube session — don't.
