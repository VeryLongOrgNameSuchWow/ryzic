<!-- PR title MUST follow Conventional Commits (release-please reads it):
     <type>[scope][!]: <short description>
     e.g.  feat(commands): add /shuffle
           fix(cache): handle missing video_id
           docs(readme): clarify setup -->

## Summary

<!-- One paragraph: what changes and why. -->

## What's in / what's out

<!-- Optional. Use if scope was non-obvious or you deferred related work. -->

## Test plan

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run ty check`
- [ ] (if integration-relevant) `uv run pytest -q -m integration`
- [ ] CI on this PR — green

## Closes / refs

<!-- "Closes #N" footers in the squash-merge body auto-close issues at
     release time. Use them. -->

Closes #
