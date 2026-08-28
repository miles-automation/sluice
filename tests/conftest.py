import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sluice.config import Config, Limits, ServerConfig
from sluice.proxy import Proxy, connect

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_config() -> Config:
    """Spawns the fake downstream server as a real stdio subprocess.

    `-m` puts cwd on sys.path, so running from the repo root makes the `tests`
    package importable in the child without any PYTHONPATH plumbing.
    """
    return Config(
        server=ServerConfig(
            name="fake",
            command=sys.executable,
            args=["-m", "tests.fake_server"],
            cwd=str(REPO_ROOT),
        ),
        limits=Limits(),
    )


@pytest.fixture
async def proxy(fake_config: Config) -> AsyncIterator[Proxy]:
    async with connect(fake_config) as started:
        yield started
