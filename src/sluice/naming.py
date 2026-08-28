"""Injective naming for mounted tools and tables (spec 3.2).

Pure module: no MCP imports, no DuckDB imports, no IO.

Sanitizing alone is not injective. MCP tool names are case-sensitive and may
contain hyphens and dots, so `a-b` and `a_b`, or `Foo` and `foo`, collapse to the
same string under naive slugging and would silently share a table. Every name
therefore carries a hash tag derived from the *unsanitized* identity.
"""

import hashlib
import re

MAX_MOUNTED_NAME = 128
"""MCP's recommended ceiling for a tool name. A mounted name that exceeds it
fails startup loudly rather than being truncated into a collision."""

SLUG_MAX = 40
TAG_LEN = 12
"""48 bits. A 6-character tag is 24 bits, and a brute-force search found a real
collision after ~3,300 candidate tool names (`a..---_` and `a-----_-_` both
mounted as `s__a__fa29cc`), which is exactly the birthday bound.

Widening lowers the probability. It does not prove injectivity, so it is not the
fix on its own: `assert_injective` below is, because it turns a collision into a
loud startup failure instead of one tool silently overwriting another.
"""

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class NameTooLongError(ValueError):
    """A mounted tool name would exceed MAX_MOUNTED_NAME."""


class NameCollisionError(ValueError):
    """Two distinct downstream tools mounted to the same name."""


def slug(value: str) -> str:
    """Lowercase, collapse non-alphanumerics to underscore, bound the length."""
    collapsed = _NON_ALNUM.sub("_", value.lower()).strip("_")
    return collapsed[:SLUG_MAX] or "x"


def tag(value: str) -> str:
    """Short stable digest of an exact, unsanitized identity."""
    return hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()[:TAG_LEN]


def identity(server: str, tool: str) -> str:
    """The exact identity a tag is taken over. NUL-joined so it is unambiguous."""
    return f"{server}\x00{tool}"


def mounted_name(server: str, tool: str) -> str:
    """The name Sluice exposes upstream for a downstream tool."""
    name = f"{slug(server)}__{slug(tool)}__{tag(identity(server, tool))}"
    if len(name) > MAX_MOUNTED_NAME:
        raise NameTooLongError(
            f"mounted name for {server}/{tool} is {len(name)} chars, "
            f"over the {MAX_MOUNTED_NAME} limit"
        )
    return name


def table_name(mounted: str, scope_tag: str, seq: int) -> str:
    """Flat-table name (spec 3.2).

    The scope tag is what stops a stale handle from a previous process resolving
    to a live table holding different data: sequence numbers restart at 1 on
    every process start, so without it a resumed conversation could query
    `..._0001` and get a clean answer about someone else's result set.
    """
    return f"{mounted}__{scope_tag}__{seq:04d}"


def quote_ident(name: str) -> str:
    """Quote a SQL identifier. Applied to every generated identifier, no exceptions."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def assert_injective(pairs: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Map (server, tool) pairs to mounted names, refusing any collision.

    A hash tag makes collisions unlikely, never impossible. Silently letting one
    tool overwrite another in a dict is the failure mode this exists to stop:
    the agent would call a tool it can see and reach a different one.
    """
    seen: dict[str, tuple[str, str]] = {}
    for server, tool in pairs:
        name = mounted_name(server, tool)
        clash = seen.get(name)
        if clash is not None and clash != (server, tool):
            raise NameCollisionError(
                f"{clash[0]}/{clash[1]!r} and {server}/{tool!r} both mount as {name!r}"
            )
        seen[name] = (server, tool)
    return seen
