import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from mcp import Client

from sluice.config import Config, Limits, ServerConfig
from sluice.intercept import Interceptor
from sluice.proxy import Proxy, connect
from sluice.store import Store

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


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(Limits()) as opened:
        yield opened


@pytest.fixture
def interceptor(store: Store) -> Interceptor:
    return Interceptor(store, Limits())


@pytest.fixture
async def sluice_client(fake_config: Config, store: Store) -> AsyncIterator[Client]:
    """A client talking to the real Sluice server WITH interception on.

    Distinct from the `proxy` fixture on purpose: whole-model passthrough
    assertions made only against `Proxy.call` left the rest of the product path
    unguarded, and a mutation stripping result `_meta` inside the interceptor or
    the upstream server passed the entire suite.
    """
    from sluice.server import build_server

    async with AsyncExitStack() as stack:
        started = await Proxy.start(fake_config, stack)
        server = build_server(started, Interceptor(store, fake_config.limits))
        async with Client(server) as client:
            yield client
