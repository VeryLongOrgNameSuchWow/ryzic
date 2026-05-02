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

import shutil
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lavalink import Client, LoadType

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

    # pytest tmpdirs are 0700; Lavalink (uid 322) needs world-read on dirs
    # and write on plugins/ for its first-boot jar download.
    for d in (config_dir, plugins_dir, cache_dir):
        d.chmod(0o755)
    plugins_dir.chmod(0o777)

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
        result = await node.get_tracks(f"{CACHE_PATH_IN_CONTAINER}/{FIXTURE.name}")
    finally:
        await client.close()

    assert result.load_type is LoadType.TRACK, (
        f"expected TRACK, got {result.load_type!r} (error={result.error!r})"
    )
    assert result.tracks, "Lavalink returned no tracks for local file"
