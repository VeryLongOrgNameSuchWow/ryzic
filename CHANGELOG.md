# Changelog

## 0.1.0 (2026-05-03)


### Features

* **audio:** lavalink.py wire-up + voice bridge ([#3](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/3)) ([0bb17f0](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/0bb17f065d3661fa5bad675795c88a67e0cdfb5e))
* **cache:** audio cache (sqlite-LRU + per-video lock) ([#6](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/6)) ([0234b91](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/0234b91f77fcbd95e6c03c1ded96281ddd10dc66))
* **cache:** playlist metadata cache (live-first with TTL fallback) ([#5](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/5)) ([67ae09f](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/67ae09f892743d0c902ffeaaf796816e88b201ba))
* **commands:** /play + ux + voice helper ([#16](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/16)) ([f952c4d](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/f952c4dc84a2100ee16475202e42a398a7e18d7d))
* **commands:** /skip /queue /pause /resume /leave + remove /lltest ([#23](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/23)) ([4e98837](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/4e98837f3d5f9bd0cff0705686ec3c6219eb64ab))
* **deploy:** Dockerfile + docker-compose + Lavalink config ([#4](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/4)) ([3e8d1b4](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/3e8d1b4e47d23ac4fea9f663190844207afd064f))
* **deploy:** pull-only compose.yaml for v0.1.0 GHCR consumers ([#37](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/37)) ([fe7cff6](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/fe7cff6ddb0b42d9aae90f660410b7b07ff829da))
* project skeleton, /ping bot, release-please, CI, dependabot ([#1](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/1)) ([40b6a57](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/40b6a57a76fb3ef0c5c94e17f9fbd68b37ec3749))
* **release:** SLSA build provenance attestation on GHCR push ([#38](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/38)) ([387f1b3](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/387f1b30fbcbc9ba86b2dee104238612a84fcc44))
* **ytdlp:** wrapper + URL validator + tests ([#2](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/2)) ([e0305ac](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/e0305acb9497f03873ede8c88d35ca1e002906ea))


### Bug Fixes

* **audio-cache:** release pins for queued tracks on queue-clear paths ([#28](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/28)) ([3474fb1](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/3474fb10771e90f6424e2e4dd54c03ad0a2af696))
* **audio-cache:** release queued pins on guild-leave teardown path ([#31](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/31)) ([dd86cc0](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/dd86cc0ec9c74e0b7035c6571ee835f54841d2c4))
* **ci:** exempt yt-dlp from Dependabot auto-merge to match dependabot.yml intent ([#34](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/34)) ([9e8584f](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/9e8584f3b4938b9fcb86350f8cdd00205abd0c0d))
* **deploy:** authenticate Lavalink healthcheck against /version ([#46](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/46)) ([cdf5141](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/cdf5141578e814e45ce975bd72f040337f300de9))
* **release-please:** re-add bump-minor-pre-major to honor SEMVER.md pre-1.0 promise ([#27](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/27)) ([37532c1](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/37532c1bbfb256254827b641b70e3e1b1d569c3a))
* **release:** read digest from build step, not metadata-action ([#43](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/43)) ([85d4fbf](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/85d4fbf5f051b913e9729caed4dd92bb4bbc3461))


### Documentation

* **deploy:** fold remaining /review nits from PR [#37](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/37) ([#42](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/42)) ([a32c772](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/a32c772029e50beb1dba6db513f7836ff82af057))
* README + SEMVER + manual smoke tests ([#7](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/7)) ([bd659cc](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/bd659cc9e9ec912899683744e40c6384b892a670))
* **readme:** add CodeQL + latest-release badges for the public repo ([#32](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/32)) ([ad38cd9](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/ad38cd9958cbba7e92ca08c087c548b06cf61316))
* **semver,contributing:** align bump table with feat: → minor pre-1.0 ([#20](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/20)) ([b2bf513](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/b2bf51333b925cbbb2fa7c7c4d7f14ed0348a18e))
