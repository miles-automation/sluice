"""Configuration loading (spec 7)."""

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_ENV_VAR = "SLUICE_CONFIG"
DEFAULT_CONFIG_NAME = "sluice.toml"

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """The configuration file is missing, malformed, or unsupported."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None


_POSITIVE = (
    "max_payload_bytes",
    "max_concurrent_materializations",
    "max_columns",
    "query_max_rows",
    "query_max_bytes",
    "max_cell_bytes",
)
_NON_NEGATIVE = ("preview_bytes", "preview_rows")

SESSION_RETENTION_DEFAULT = 256 * 1024 * 1024
SESSION_CALLS_DEFAULT = 1000
QUERY_OUTPUT_MIN_BYTES = 384
CELL_OUTPUT_MIN_BYTES = 4


@dataclass(frozen=True, slots=True)
class Limits:
    # Post-R11 calibration: a 1 MiB dual-channel call takes about 7.6 seconds
    # and peaks 56 MiB above the decoded-input baseline on the reference host.
    # The former 32 MiB default took minutes near its ceiling and was not an
    # operationally defensible admission limit.
    max_payload_bytes: int = 1_048_576
    max_concurrent_materializations: int = 2
    preview_bytes: int = 2048
    preview_rows: int = 3
    max_columns: int = 64
    query_timeout_seconds: float = 10.0
    query_max_rows: int = 100
    query_max_bytes: int = 65_536
    max_cell_bytes: int = 512
    duckdb_max_memory: str = "1GB"
    # This is a logical retained-state budget, not a process RSS guarantee.
    # Keeping it finite by default prevents a long-lived session from growing
    # without bound; it is calibrated independently from per-call admission.
    max_session_bytes: int = SESSION_RETENTION_DEFAULT
    max_session_calls: int = SESSION_CALLS_DEFAULT

    def __post_init__(self) -> None:
        # Validated here rather than at parse time so a Limits built in code is
        # checked too. `max_concurrent_materializations = 0` is the sharp one:
        # a zero-sized admission semaphore blocks every materialization forever
        # rather than failing.
        #
        # Types are checked before values because TOML hands us strings, floats,
        # and booleans that reach here happily: `max_payload_bytes = "100"` used
        # to raise an uncaught TypeError from the comparison below, and
        # `max_columns = true` was accepted because bool subclasses int.
        for name in (*_POSITIVE, "max_session_bytes", "max_session_calls", *_NON_NEGATIVE):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(
                    f"[limits].{name} must be an integer, got {type(value).__name__}: {value!r}"
                )
        for name in (*_POSITIVE, "max_session_bytes", "max_session_calls"):
            if getattr(self, name) < 1:
                raise ConfigError(f"[limits].{name} must be at least 1, got {getattr(self, name)}")
        for name in _NON_NEGATIVE:
            if getattr(self, name) < 0:
                raise ConfigError(
                    f"[limits].{name} must not be negative, got {getattr(self, name)}"
                )

        if self.max_session_bytes < self.max_payload_bytes:
            raise ConfigError(
                "[limits].max_session_bytes must be at least max_payload_bytes "
                f"({self.max_payload_bytes}), got {self.max_session_bytes}"
            )
        if self.query_max_bytes < QUERY_OUTPUT_MIN_BYTES:
            raise ConfigError(
                f"[limits].query_max_bytes must be at least {QUERY_OUTPUT_MIN_BYTES} "
                "so truncation notices cannot be truncated themselves"
            )
        if self.max_cell_bytes < CELL_OUTPUT_MIN_BYTES:
            raise ConfigError(
                f"[limits].max_cell_bytes must be at least {CELL_OUTPUT_MIN_BYTES} "
                "so a truncated cell can carry its ellipsis"
            )

        timeout = self.query_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise ConfigError(
                f"[limits].query_timeout_seconds must be a number, "
                f"got {type(timeout).__name__}: {timeout!r}"
            )
        if not math.isfinite(timeout):
            raise ConfigError(f"[limits].query_timeout_seconds must be finite, got {timeout}")
        if timeout <= 0:
            raise ConfigError(f"[limits].query_timeout_seconds must be positive, got {timeout}")

        if not isinstance(self.duckdb_max_memory, str):
            raise ConfigError(
                f"[limits].duckdb_max_memory must be a string, "
                f"got {type(self.duckdb_max_memory).__name__}"
            )
        if not self.duckdb_max_memory.strip():
            raise ConfigError("[limits].duckdb_max_memory must not be empty")


@dataclass(frozen=True, slots=True)
class Config:
    server: ServerConfig
    limits: Limits


def find_config(explicit: Path | None = None) -> Path:
    """`--config`, then $SLUICE_CONFIG, then ./sluice.toml."""
    if explicit is not None:
        return explicit
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    return Path(DEFAULT_CONFIG_NAME)


def expand_vars(value: str, environ: dict[str, str] | None = None) -> str:
    """Expand ${VAR} from the process environment. Secrets stay out of the file."""
    env = os.environ if environ is None else environ

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        found = env.get(name)
        if found is None:
            raise ConfigError(f"config references ${{{name}}}, which is not set")
        return found

    return _VAR.sub(replace, value)


def load_config(path: Path, environ: dict[str, str] | None = None) -> Config:
    if not path.exists():
        raise ConfigError(f"no config file at {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_config(raw, environ=environ)


def parse_config(raw: Mapping[str, Any], environ: dict[str, str] | None = None) -> Config:
    servers_raw = raw.get("servers")
    if not isinstance(servers_raw, dict) or not servers_raw:
        raise ConfigError("config must define at least one [servers.<name>] table")

    # FR-7: the config format admits several because fan-out is coming, but v0
    # refuses to start with more than one rather than pretending to support it.
    if len(servers_raw) > 1:
        names = ", ".join(sorted(servers_raw))
        raise ConfigError(
            f"v0 proxies exactly one downstream server; found {len(servers_raw)} ({names}). "
            "Tool namespacing is built for fan-out, but fan-out is not implemented."
        )

    name, entry = next(iter(servers_raw.items()))
    if not isinstance(entry, dict):
        raise ConfigError(f"[servers.{name}] must be a table")

    command = entry.get("command")
    url = entry.get("url")
    if command is None and url is None:
        raise ConfigError(f"[servers.{name}] needs either `command` or `url`")
    if command is not None and url is not None:
        raise ConfigError(f"[servers.{name}] sets both `command` and `url`; they are exclusive")
    if url is not None:
        raise ConfigError(
            f"[servers.{name}] sets `url`; v0 speaks stdio only, so `command` is required"
        )
    if not isinstance(command, str):
        raise ConfigError(f"[servers.{name}].command must be a string")

    args_raw = entry.get("args", [])
    if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
        raise ConfigError(f"[servers.{name}].args must be a list of strings")

    env_raw = entry.get("env", {})
    if not isinstance(env_raw, dict):
        raise ConfigError(f"[servers.{name}].env must be a table")
    env = {str(k): expand_vars(str(v), environ=environ) for k, v in env_raw.items()}

    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ConfigError(f"[servers.{name}].cwd must be a string")

    server = ServerConfig(
        name=name,
        command=expand_vars(command, environ=environ),
        args=[expand_vars(a, environ=environ) for a in args_raw],
        env=env,
        cwd=cwd,
    )

    limits_raw = raw.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise ConfigError("[limits] must be a table")
    known = set(Limits.__dataclass_fields__)
    unknown = set(limits_raw) - known
    if unknown:
        raise ConfigError(f"[limits] has unknown keys: {', '.join(sorted(unknown))}")
    limits = Limits(**limits_raw)

    return Config(server=server, limits=limits)
