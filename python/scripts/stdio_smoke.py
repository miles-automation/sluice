import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters, types

PYTHON_ROOT = Path(__file__).resolve().parents[1]


async def smoke(config: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sluice", "--config", str(config)],
        cwd=str(PYTHON_ROOT),
    )
    with anyio.fail_after(120):
        async with Client(params) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "query" in names, names
            rows = next(name for name in names if name.startswith("fake__rows__"))
            handle = await client.call_tool(rows, {"n": 5})
            assert handle.structured_content is not None, handle
            table = handle.structured_content["tables"][0]["name"]
            result = await client.call_tool("query", {"sql": f'SELECT count(*) FROM "{table}"'})
            block = result.content[0]
            assert isinstance(block, types.TextContent) and "| 5 |" in block.text, result


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "sluice.toml"
        config.write_text(
            "[servers.fake]\n"
            f"command = '{sys.executable}'\n"
            "args = ['-m', 'tests.fake_server']\n"
            f"cwd = '{PYTHON_ROOT}'\n",
            encoding="utf-8",
        )
        anyio.run(smoke, config)
    print("stdio smoke passed", file=sys.stderr)


if __name__ == "__main__":
    main()
