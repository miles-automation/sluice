"""Configuration loading and limit validation (spec 7)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sluice.config import Config, ConfigError, Limits, find_config, load_config, parse_config

MINIMAL: dict[str, Any] = {"servers": {"gh": {"command": "echo"}}}


def test_minimal_config_parses() -> None:
    config = parse_config(MINIMAL)
    assert config.server.name == "gh"
    assert config.server.command == "echo"
    assert config.limits == Limits()


def test_env_expansion() -> None:
    raw: dict[str, Any] = {"servers": {"gh": {"command": "echo", "env": {"TOKEN": "${SECRET}"}}}}
    config = parse_config(raw, environ={"SECRET": "s3cret"})
    assert config.server.env == {"TOKEN": "s3cret"}


def test_missing_env_var_is_an_error() -> None:
    raw: dict[str, Any] = {"servers": {"gh": {"command": "echo", "env": {"TOKEN": "${ABSENT}"}}}}
    with pytest.raises(ConfigError, match="ABSENT"):
        parse_config(raw, environ={})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "at least one"),
        ({"servers": {}}, "at least one"),
        ({"servers": {"a": {"command": "x"}, "b": {"command": "y"}}}, "exactly one"),
        ({"servers": {"a": {}}}, "either `command` or `url`"),
        ({"servers": {"a": {"command": "x", "url": "http://h"}}}, "exclusive"),
        ({"servers": {"a": {"url": "http://h"}}}, "stdio only"),
        ({"servers": {"a": {"command": 3}}}, "must be a string"),
        ({"servers": {"a": {"command": "x", "args": "no"}}}, "list of strings"),
        ({"servers": {"a": {"command": "x"}}, "limits": {"nope": 1}}, "unknown keys"),
    ],
)
def test_rejected_configs(raw: Mapping[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # The sharp one: a zero-sized admission semaphore blocks every
        # materialization forever instead of failing.
        ("max_concurrent_materializations", 0),
        ("max_payload_bytes", -1),
        ("max_columns", 0),
        ("query_max_rows", 0),
        ("query_max_bytes", 0),
        ("max_cell_bytes", 0),
        ("preview_bytes", -1),
        ("preview_rows", -1),
        ("query_timeout_seconds", 0),
        ("query_timeout_seconds", -5),
    ],
)
def test_rejected_limits(field: str, value: int) -> None:
    with pytest.raises(ConfigError, match=field):
        parse_config({**MINIMAL, "limits": {field: value}})


def test_limits_are_validated_when_built_in_code_too() -> None:
    """Validation lives on the dataclass, not only in the parser, so a Limits
    constructed by a caller cannot smuggle a deadlock in."""
    with pytest.raises(ConfigError):
        Limits(max_concurrent_materializations=0)


def test_empty_memory_limit_is_rejected() -> None:
    with pytest.raises(ConfigError, match="duckdb_max_memory"):
        parse_config({**MINIMAL, "limits": {"duckdb_max_memory": "  "}})


def test_load_config_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "sluice.toml"
    path.write_text('[servers.gh]\ncommand = "echo"\nargs = ["hi"]\n', encoding="utf-8")
    config = load_config(path)
    assert config.server.args == ["hi"]


def test_load_config_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no config file"):
        load_config(tmp_path / "absent.toml")


def test_load_config_reports_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "sluice.toml"
    path.write_text("this is not toml =", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_find_config_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.toml"
    monkeypatch.setenv("SLUICE_CONFIG", str(tmp_path / "from-env.toml"))
    assert find_config(explicit) == explicit
    assert find_config(None) == tmp_path / "from-env.toml"
    monkeypatch.delenv("SLUICE_CONFIG")
    assert find_config(None) == Path("sluice.toml")


def test_the_example_config_is_valid() -> None:
    """The file users are told to copy has to actually load."""
    example = Path(__file__).resolve().parents[1] / "sluice.example.toml"
    config = load_config(example, environ={"GITHUB_TOKEN": "x"})
    assert isinstance(config, Config)
