"""Scope derivation (spec 12)."""

import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from sluice import naming, scope

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_minted_scopes_are_distinct_and_full_width() -> None:
    minted = {scope.mint() for _ in range(100)}
    assert len(minted) == 100
    assert all(len(tag) == scope.SCOPE_TAG_LENGTH for tag in minted)


def test_minted_scopes_carry_capability_strength_entropy() -> None:
    """Distinctness is not unguessability: an incrementing counter is perfectly
    distinct. Scope tags are the only thing between one conversation and
    another's tables, so they need real width."""
    assert scope.SCOPE_TAG_LENGTH * 4 >= 128


def test_minting_draws_from_secrets_not_random(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the dependency itself. A predictable generator satisfies every
    "the values differ" assertion while providing no isolation at all."""
    calls: list[int] = []

    def fake_token_hex(n: int) -> str:
        calls.append(n)
        return "ab" * n

    monkeypatch.setattr(secrets, "token_hex", fake_token_hex)
    assert scope.mint() == "ab" * (scope.SCOPE_TAG_LENGTH // 2)
    assert calls == [scope.SCOPE_TAG_LENGTH // 2]


def test_conversation_id_gives_a_stable_scope() -> None:
    assert scope.from_conversation_id("conv-1") == scope.from_conversation_id("conv-1")
    assert scope.from_conversation_id("conv-1") != scope.from_conversation_id("conv-2")


def test_conversation_scope_matches_a_fixed_digest_vector() -> None:
    """A literal expected value, so swapping BLAKE2 for anything else fails
    here instead of silently changing every scope in the fleet."""
    assert (
        scope.from_conversation_id("conv-1")
        == (hashlib.blake2b(b"conv-1", digest_size=16).hexdigest()[: scope.SCOPE_TAG_LENGTH])
    )


def test_conversation_scope_is_stable_across_processes() -> None:
    """Python's builtin hash() is seed-randomized per process. Using it here
    would pass every in-process test while orphaning every table belonging to a
    resumed conversation."""
    program = "from sluice import scope; print(scope.from_conversation_id('conv-1'))"
    outputs = {
        subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert outputs == {scope.from_conversation_id("conv-1")}


def test_derive_prefers_a_client_supplied_conversation_id() -> None:
    tag, from_client = scope.derive({"conversationId": "abc"})
    assert from_client
    assert tag == scope.from_conversation_id("abc")


def test_derive_mints_when_the_client_supplies_nothing() -> None:
    first, from_client = scope.derive(None)
    second, _ = scope.derive({})
    assert not from_client
    assert first != second


def test_derive_ignores_non_string_and_empty_values() -> None:
    _, from_client = scope.derive({"conversationId": ""})
    assert not from_client
    _, from_client = scope.derive({"conversationId": 42})
    assert not from_client


def test_a_stale_handle_cannot_name_a_live_table() -> None:
    """The unconditional half of spec 12.

    Sequence numbers restart at 1 on every process start. Without a scope tag in
    the name, a resumed conversation could query `..._0001` and get a clean
    answer about a different result set.
    """
    mounted = naming.mounted_name("gh", "list_issues")
    previous_process = naming.table_name(mounted, scope.mint(), 1)
    this_process = naming.table_name(mounted, scope.mint(), 1)
    assert previous_process != this_process
