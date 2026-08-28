"""Scope derivation (spec 12)."""

from sluice import naming, scope


def test_minted_scopes_are_unguessable_and_distinct() -> None:
    minted = {scope.mint() for _ in range(100)}
    assert len(minted) == 100
    assert all(len(tag) == scope.SCOPE_TAG_LENGTH for tag in minted)


def test_conversation_id_gives_a_stable_scope() -> None:
    assert scope.from_conversation_id("conv-1") == scope.from_conversation_id("conv-1")
    assert scope.from_conversation_id("conv-1") != scope.from_conversation_id("conv-2")


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
