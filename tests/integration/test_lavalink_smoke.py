"""Smoke test for the deployment stack.

Spins a real Lavalink container with our pinned image + `application.yml` and
verifies that loading a local audio file via REST succeeds. This guards two
things that can only fail at the container boundary:

1. The Lavalink config actually enables `LocalAudioSourceManager`.
2. The shared `/var/cache/ryzic` volume contract holds — Lavalink can read a
   path the bot would write.

Opt-in: requires Docker + the network access to pull the image, hence the
`integration` marker. `pytest -q` skips it; `pytest -m integration` runs it.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lavalink import Client, LavalinkError, LoadType

pytestmark = pytest.mark.integration

LAVALINK_IMAGE = "ghcr.io/lavalink-devs/lavalink:4.2.2"
LAVALINK_PORT = 2333
LAVALINK_PASSWORD = "youshallnotpass"
CACHE_PATH_IN_CONTAINER = "/var/cache/ryzic"
FIXTURE = Path(__file__).parent / "fixtures" / "silence.ogg"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe_docker() -> bool:
    # The CLI binary is irrelevant — testcontainers talks to the daemon socket
    # directly (DOCKER_HOST or unix:///var/run/docker.sock by default), which
    # also happens to work with rootless Podman's compatible socket.
    try:
        from docker.errors import DockerException  # type: ignore[import-untyped]
        from testcontainers.core.docker_client import DockerClient
    except ImportError:
        return False
    try:
        DockerClient().client.ping()
    except DockerException:
        return False
    return True


@pytest.fixture(scope="module")
def lavalink_container(tmp_path_factory: pytest.TempPathFactory):
    if not _probe_docker():
        pytest.skip("Docker daemon not reachable")
    if not FIXTURE.exists():
        pytest.skip(f"Audio fixture missing: {FIXTURE}")

    # Stage application.yml into our own directory so we control permissions
    # (the repo file may live under restricted parent dirs in some sandboxes).
    config_dir = tmp_path_factory.mktemp("ryzic_lavalink_config")
    shutil.copy(REPO_ROOT / "lavalink" / "application.yml", config_dir / "application.yml")
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir()

    cache_dir = tmp_path_factory.mktemp("ryzic_cache")
    shutil.copy(FIXTURE, cache_dir / FIXTURE.name)

    # Lavalink runs as uid 322 inside the container. Bind mounts must be
    # readable (and the plugins dir writable) by that uid; pytest tmpdirs
    # default to 0700, which silently yields a permission-denied container
    # crash or an EMPTY load result.
    for d in (config_dir, plugins_dir, cache_dir):
        d.chmod(0o755)
    (config_dir / "application.yml").chmod(0o644)
    (cache_dir / FIXTURE.name).chmod(0o644)
    plugins_dir.chmod(0o777)  # Lavalink needs to drop the downloaded jar here.

    # `:Z` is a no-op on Docker hosts without SELinux; on Podman + SELinux
    # (Fedora) it relabels the mount so the in-container lavalink uid can read.
    # First boot also fetches the youtube-source plugin from Maven (~30s cold).
    container = (
        DockerContainer(LAVALINK_IMAGE)
        .with_env("_JAVA_OPTIONS", "-Xmx256m")
        .with_env("SERVER_PORT", str(LAVALINK_PORT))
        .with_env("LAVALINK_SERVER_PASSWORD", LAVALINK_PASSWORD)
        .with_exposed_ports(LAVALINK_PORT)
        .with_volume_mapping(
            str(config_dir / "application.yml"), "/opt/Lavalink/application.yml", "ro,Z"
        )
        .with_volume_mapping(str(plugins_dir), "/opt/Lavalink/plugins", "rw,Z")
        .with_volume_mapping(str(cache_dir), CACHE_PATH_IN_CONTAINER, "ro,Z")
        .waiting_for(LogMessageWaitStrategy("Lavalink is ready to accept connections"))
    )

    container.start()
    try:
        yield container, cache_dir
    finally:
        container.stop()


async def test_lavalink_loads_local_file(lavalink_container) -> None:
    container, _cache_dir = lavalink_container
    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(LAVALINK_PORT))

    client = Client(user_id=1)
    try:
        node = client.add_node(
            host=host,
            port=port,
            password=LAVALINK_PASSWORD,
            region="us",
            connect=False,
        )
        result = await _load_with_retry(node, f"{CACHE_PATH_IN_CONTAINER}/{FIXTURE.name}")
    finally:
        await client.close()

    assert result.load_type is LoadType.TRACK, (
        f"expected TRACK, got {result.load_type!r} (error={result.error!r})"
    )
    assert result.tracks, "Lavalink returned no tracks for local file"


async def _load_with_retry(node, query: str, deadline_s: float = 10.0):
    # `loadtracks` settles a moment after the readiness log line; one short
    # retry loop covers that race without slowing the happy path.
    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await node.get_tracks(query)
        except LavalinkError as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    raise AssertionError(f"loadtracks never succeeded within {deadline_s}s: {last_error!r}")
