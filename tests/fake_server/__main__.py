"""Run the fake downstream server over stdio: `python -m tests.fake_server`."""

import anyio
from mcp.server import NotificationOptions
from mcp.server.stdio import stdio_server

from tests.fake_server.server import build_fake_server


async def _serve() -> None:
    server = build_fake_server()
    options = server.create_initialization_options(NotificationOptions(tools_changed=False))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    anyio.run(_serve)
