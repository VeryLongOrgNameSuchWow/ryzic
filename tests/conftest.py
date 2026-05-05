"""Test-wide state hygiene.

The ``now_playing`` controller registry is process-global (mirrors the
``lavalink_glue`` singletons). Test order is randomised, so any module
that writes a controller record must reset the registry afterwards or
risk leaking the (channel_id, message_id) into an unrelated test that
calls ``now_playing.refresh`` and tries to hit a ``FakeBot`` that has
no ``rest`` attribute. Resetting in a session-wide autouse fixture
removes the per-file-fixture drift hazard.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ryzic import now_playing


@pytest.fixture(autouse=True)
def _reset_now_playing_controllers() -> Iterator[None]:
    now_playing._reset_state_for_test()
    yield
    now_playing._reset_state_for_test()
