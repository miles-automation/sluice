"""The real application boundary: a Sluice subprocess speaking stdio.

Every other test builds `Config` in code and connects to `build_server(...)`
in-process. That leaves `__main__.py` and most of `config.py` unexercised, so a
broken entrypoint, a bad TOML path, or a startup regression would not fail the
suite. This file launches the actual installed command.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters, types

from sluice import naming

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, command: str = sys.executable) -> Path:
    path = tmp_path / "sluice.toml"
    path.write_text(
        "[servers.fake]\n"
        f'command = "{command}"\n'
        'args = ["-m", "tests.fake_server"]\n'
        f'cwd = "{REPO_ROOT}"\n'
        "\n[limits]\npreview_bytes = 512\n",
        encoding="utf-8",
    )
    return path


async def test_sluice_runs_as_a_real_stdio_server(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "sluice", "--config", str(config)], cwd=str(REPO_ROOT)
    )
    async with Client(params) as client:
        listing = await client.list_tools()
        mounted = naming.mounted_name("fake", "rows")
        assert mounted in {tool.name for tool in listing.tools}

        result = await client.call_tool(mounted, {"n": 400})
        block = result.content[0]
        assert isinstance(block, types.TextContent)
        # The whole point, proven through the real binary: a handle, not 400 rows.
        assert "sluice: result recorded." in block.text
        assert "row-0399" not in block.text
        assert result.structured_content is not None
        assert result.structured_content["tables"][0]["row_count"] == 400


def test_missing_config_exits_with_a_diagnostic(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "sluice", "--config", str(tmp_path / "absent.toml")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "no config file" in completed.stderr
    assert completed.stdout == ""


def test_invalid_limits_exit_with_a_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "sluice.toml"
    path.write_text(
        '[servers.fake]\ncommand = "echo"\n\n[limits]\nmax_concurrent_materializations = 0\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "sluice", "--config", str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "max_concurrent_materializations" in completed.stderr


def test_unreachable_downstream_fails_to_start(tmp_path: Path) -> None:
    """FR-6: fail to start rather than run in a degraded state."""
    config = _write_config(tmp_path, command="definitely-not-a-real-command")
    completed = subprocess.run(
        [sys.executable, "-m", "sluice", "--config", str(config)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert completed.returncode == 1
    # A clean one-line diagnostic, not a traceback. Asserting only on the exit
    # code would pass just as happily on an unhandled crash.
    assert "could not start downstream" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


def test_startup_errors_are_unwrapped_from_exception_groups() -> None:
    """anyio wraps failures raised inside a task group, so the entrypoint has to
    look inside one to report the real cause."""
    from sluice.__main__ import first_startup_error
    from sluice.errors import DownstreamError, FailureClass

    inner = DownstreamError(FailureClass.TRANSPORT, "fake", "boom")
    group = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [inner])])
    assert first_startup_error(group) is inner
    assert first_startup_error(ValueError("unrelated")) is None
