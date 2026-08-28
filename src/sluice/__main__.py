"""Entrypoint: `sluice --config sluice.toml`, speaking MCP over stdio."""

import argparse
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from sluice.config import Config, ConfigError, find_config, load_config
from sluice.errors import DownstreamError
from sluice.intercept import Interceptor
from sluice.proxy import Proxy
from sluice.server import build_server, initialization_options
from sluice.store import Store


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sluice", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="path to sluice.toml")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


async def serve(config: Config) -> None:
    async with AsyncExitStack() as stack:
        proxy = await Proxy.start(config, stack)
        store = stack.enter_context(Store.open(config.limits))
        interceptor = Interceptor(store, config.limits)
        server = build_server(proxy, interceptor)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, initialization_options(server))  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # stdout is the MCP transport. Everything diagnostic goes to stderr.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(find_config(args.config))
    except ConfigError as exc:
        print(f"sluice: {exc}", file=sys.stderr)
        return 2
    try:
        anyio.run(serve, config)
    except DownstreamError as exc:
        # FR-6: fail to start rather than running in a degraded state.
        print(f"sluice: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
