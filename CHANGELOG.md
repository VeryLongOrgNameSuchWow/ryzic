# Changelog

## [0.6.0](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.5.4...v0.6.0) (2026-06-23)


### Features

* **controller:** rename "Leave" button to "Stop & leave" ([#176](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/176)) ([b37f634](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/b37f6342d3b4569273cde8129fc9d929ddde6ecd)), closes [#174](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/174)
* **i18n:** catalog-drift CI lint + CONTRIBUTING.md i18n section ([#164](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/164)) ([bcbcb26](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/bcbcb26152000add9fef34271004f8e1824fd566))
* **i18n:** migrate lavalink_glue broadcasts ([#162](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/162)) ([1ec9b25](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/1ec9b25d943c8e6d839968582c772add66119019))
* **i18n:** migrate now_playing controller + button labels; rename Stop button to Leave ([#147](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/147)) ([#161](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/161)) ([bac5752](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/bac57523506798da71a56d5807039e3183804fb3))
* **i18n:** migrate np + seek; rename /np to /nowplaying ([#150](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/150)); rewrite /seek param description ([#151](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/151)) ([#158](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/158)) ([fb46224](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/fb46224da2671fd53f5a7deb994970f8c6308e6c))
* **i18n:** migrate play.py (catalog only) ([#160](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/160)) ([59a35a6](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/59a35a6af1dc8c6812daf67a5198891ac2d8a0f4))
* **i18n:** migrate queue + flip ephemeral default ([#148](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/148)) ([#159](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/159)) ([b52d49a](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/b52d49a9ee7d1fe104b417c2551e5bdca9a2ca1b))
* **i18n:** migrate small commands bundle ([#157](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/157)) ([38756d9](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/38756d990e2837fad20e4002eecae6e754239fc1))
* **i18n:** migrate ux.py builders to catalog ([#155](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/155)) ([950821c](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/950821cc6f1282338a32d67a116f073f599168fd))
* **i18n:** migrate voice_check + shared error keys ([#156](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/156)) ([bf30cf7](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/bf30cf7860d93d1a7dae8361b53cb7ad83db7abb))
* **i18n:** migrate ytdlp.py; change FetchFailed contract to carry key + vars ([#163](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/163)) ([5992914](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/599291427199e9a76f456d14d51a0c5830a798c7))
* **i18n:** scaffold i18nice helper + en_US catalog ([#153](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/153)) ([b1a2da0](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/b1a2da04354fc245ec992a3dd442a46a1521d2c9))
* **i18n:** separate voice-handshake-failure copy from audio-service-down ([#166](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/166)) ([429dc76](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/429dc760498cf345db156d911c70a43dab135cb0)), closes [#144](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/144)
* **i18n:** show example URL in unsupported-URL error ([#165](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/165)) ([1c95bd1](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/1c95bd120218c4b8474f86c771e53fe0a0b2efbb)), closes [#145](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/145)
* **play:** /play with no argument resumes paused track ([#167](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/167)) ([ada8060](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/ada8060a300e6fdbcdca5749f1f2c9a56c23aa32)), closes [#146](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/146)
* **play:** first-play tip-footer points newcomers at the controller buttons ([#168](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/168)) ([e5ea247](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/e5ea2472e55875839e92157fb047f4149f94848f)), closes [#152](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/152)
* **ux:** drop "by {uploader}" line from now-playing embed ([#177](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/177)) ([9f3c894](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/9f3c8941748692f9752263eee1e1a1035b69ca25)), closes [#173](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/173)


### Bug Fixes

* **commands:** align /nowplaying ephemeral default with /queue ([#207](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/207)) ([618ca0d](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/618ca0dabcb93728ae14c597091951746d1aedf6))
* **commands:** nothing-playing guard checks `current` directly ([#196](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/196)) ([3a0e62e](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/3a0e62e0d1e62e1dc6c9e226fb9e7e4684bb1c70))
* **commands:** reconnecting copy when Lavalink player disconnected ([#215](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/215)) ([2af04a4](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/2af04a4051327da17b320f5ebc7a334918ff10cd))
* **lavalink_glue:** bound and clear intentional-disconnect marker ([#203](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/203)) ([59b0022](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/59b002241b60c11c583a10b5ba78d05e6c64903a))
* **lavalink_glue:** clean up intentional-disconnect marker on error paths ([#198](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/198)) ([fc151ad](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/fc151ad30d75e41a43032d0a7422dafa8fb15b2b))
* **lavalink_glue:** skip voice_lost broadcast on intentional disconnects ([#197](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/197)) ([cf24312](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/cf24312534e52551e4de9ecd6a3c5778470f0350)), closes [#170](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/170)
* periodially refresh now-playing controllers so progress advances ([#172](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/172)) ([#200](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/200)) ([1687730](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/16877308bf813a7cd9d127055236696f9ec738ae))


### Documentation

* refresh README, smoke-tests, docstrings ([#206](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/206)) ([f28e466](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/f28e4661ca9cc3c506defc6e39315b313a9c6150))

## [0.5.4](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.5.3...v0.5.4) (2026-05-08)


### Bug Fixes

* cache-first /play; resolve_track skips format selection ([#133](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/133)) ([762820c](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/762820c818b09549ef9d7151fc87d406bb6c60a9)), closes [#132](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/132)
* pass real title and author to lavalink AudioTrack ([#137](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/137)) ([fa824dc](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/fa824dc267c31e661333f92197f52f6479eadd6b)), closes [#136](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/136)

## [0.5.3](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.5.2...v0.5.3) (2026-05-06)


### Bug Fixes

* **ci:** switch to URL-with-token form for compose-pin-auto clone ([#130](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/130)) ([f96e312](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/f96e3124b0ba02a8710153d442a7f9e69fc87165))

## [0.5.2](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.5.1...v0.5.2) (2026-05-06)


### Bug Fixes

* **ci:** use local extraheader config instead of -c for compose-pin-auto clone ([#125](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/125)) ([46f8ef6](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/46f8ef639c2817fdf0ff6155dc46bb9148b64225))

## [0.5.1](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.5.0...v0.5.1) (2026-05-06)


### Bug Fixes

* **ci:** correct extraheader URL key + remove false-on-refresh conditional in compose-pin-auto ([#123](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/123)) ([66aaedd](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/66aaedd560d4da3ee900741c5896f1512c17bdc0))

## [0.5.0](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.4.0...v0.5.0) (2026-05-06)


### Features

* add /seek &lt;m:ss|+30|-15&gt; ([#114](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/114)) ([fa7452f](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/fa7452f359245ec659485dc5bb11bc3d782d8031))
* add optional private flag to /np for ephemeral responses ([#122](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/122)) ([f925aa6](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/f925aa61594ee7df9ca0227018e6cc3f2ba95a62))
* add optional private flag to /queue for ephemeral responses ([#120](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/120)) ([5e33b4f](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/5e33b4fc3834ca6246f8ebdcafe6532cd2c83920))
* add page argument to /queue for browsing past the first 10 tracks ([#119](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/119)) ([7935db8](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/7935db8b22e9b4b0448963eb955b5ee02f2487b5))
* in-memory track history with /recent and /replay ([#116](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/116)) ([5d03b2a](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/5d03b2aeea9dde09b4d90779c295db17689a5cb2))
* persistent now-playing controller embed with media-remote buttons ([#118](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/118)) ([b2b822b](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/b2b822b81655439a4fe7fcc195af6842d7cd02f7))
* split /np from /queue with shared now-playing helper ([#115](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/115)) ([a5652da](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/a5652da469c3a4efd607992526ae8285f0e59331))

## [0.4.0](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.3.0...v0.4.0) (2026-05-05)


### ⚠ BREAKING CHANGES

* LAVALINK_PASSWORD is now required at startup. Operators relying on the previous default literal must set it explicitly in .env; the password must match between ryzic and the lavalink container.

### Features

* drop literal LAVALINK_PASSWORD fallback ([#106](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/106)) ([a6775c9](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/a6775c930aac84e03a09d86e985da4136000db57))


### Documentation

* **readme:** add cookies-mount recipe and compose.override.yaml.example ([#104](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/104)) ([9c0f145](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/9c0f145b7ebdfa64bff2f43271a118ce81f15088))

## [0.3.0](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.2.2...v0.3.0) (2026-05-04)


### Features

* **config:** add RYZIC_AUTOLEAVE_SECONDS for configurable queue-end auto-leave ([#77](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/77)) ([67449d1](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/67449d109036654a4c43fbe013f5372d8c8b4b31))
* **ux:** show channel + requester on single-track /play embed ([#75](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/75)) ([a0272c8](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/a0272c841914265a0a0747fd4fbbf60b9bdc4f5e))

## [0.2.2](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.2.1...v0.2.2) (2026-05-03)


### Documentation

* codify operator-decisions principle (CLAUDE.md + M1.md) ([#55](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/55)) ([09e11c5](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/09e11c5b6635a833edad9f0405581a97b3a5afe4))

## [0.2.1](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.2.0...v0.2.1) (2026-05-03)


### Bug Fixes

* **ci:** use commit SHA (not tag-object SHA) for codeql-action pins ([#53](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/53)) ([a0df750](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/a0df75057c8d99636e7f7dd6617f53fce7e7ac95))

## [0.2.0](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.1.1...v0.2.0) (2026-05-03)


### Features

* **ytdlp:** add opt-in RYZIC_YOUTUBE_COOKIES_PATH env var for cookies-gated content ([#51](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/51)) ([c470ae6](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/c470ae682e84fcaab8a46eb4702e6d99c92eca5c))

## [0.1.1](https://github.com/VeryLongOrgNameSuchWow/ryzic/compare/v0.1.0...v0.1.1) (2026-05-03)


### Bug Fixes

* **ytdlp:** map yt-dlp 'Requested format is not available' to friendly livestream rejection ([#48](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/48)) ([13caa53](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/13caa53ea7678c1d2cdf5fea4a977238ccb75232))


### Documentation

* **readme:** document gh attestation verify for SLSA-signed images ([#50](https://github.com/VeryLongOrgNameSuchWow/ryzic/issues/50)) ([d694b77](https://github.com/VeryLongOrgNameSuchWow/ryzic/commit/d694b777c39159a09ebc3f95b9cfabe2de019e09))

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
