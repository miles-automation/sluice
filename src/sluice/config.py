"""Configuration loading (spec 7)."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class Limits:
    max_payload_bytes: int = 33_554_432
    max_concurrent_materializations: int = 2
    preview_bytes: int = 2048
    preview_rows: int = 3
    max_columns: int = 64
    query_timeout_seconds: float = 10.0
    query_max_rows: int = 100
    query_max_bytes: int = 65_536
    max_cell_bytes: int = 512
    duckdb_max_memory: str = "1GB"


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


def parse_config(raw: dict[str, object], environ: dict[str, str] | None = None) -> Config:
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
    known = {f for f in Limits.__dataclass_fields__}
    unknown = set(limits_raw) - known
    if unknown:
        raise ConfigError(f"[limits] has unknown keys: {', '.join(sorted(unknown))}")
    limits = Limits(**limits_raw)

    return Config(server=server, limits=limits)
