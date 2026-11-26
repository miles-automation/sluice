"""Naming must be injective (spec 3.2)."""

import pytest

from sluice import naming


def test_hyphen_and_underscore_tools_do_not_collide() -> None:
    # The exact defect: `a-b` and `a_b` are distinct MCP tool names that collapse
    # to the same string under naive slugging and would share one table.
    assert naming.mounted_name("s", "hyphen-tool") != naming.mounted_name("s", "hyphen_tool")


def test_case_differing_tools_do_not_collide() -> None:
    assert naming.mounted_name("s", "Foo") != naming.mounted_name("s", "foo")


def test_server_and_tool_boundary_is_unambiguous() -> None:
    # Without a separator in the hashed identity, ("ab", "c") and ("a", "bc")
    # would hash identically.
    assert naming.mounted_name("ab", "c") != naming.mounted_name("a", "bc")


def test_mounted_name_is_stable() -> None:
    assert naming.mounted_name("gh", "list_issues") == naming.mounted_name("gh", "list_issues")


def test_mounted_name_shape() -> None:
    name = naming.mounted_name("gh", "list-issues")
    assert name.startswith("gh__list_issues__")
    assert len(name.rsplit("__", 1)[1]) == naming.TAG_LEN


def test_long_names_fail_loudly_rather_than_truncating() -> None:
    long_tool = "t" * 400
    # Slugging bounds the readable part, so this one still fits; the guard is
    # what matters if SLUG_MAX is ever raised.
    assert len(naming.mounted_name("s", long_tool)) <= naming.MAX_MOUNTED_NAME


def test_name_too_long_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(naming, "MAX_MOUNTED_NAME", 10)
    with pytest.raises(naming.NameTooLongError):
        naming.mounted_name("server", "tool")


def test_table_name_carries_scope_and_sequence() -> None:
    mounted = naming.mounted_name("gh", "list_issues")
    assert naming.table_name(mounted, "k7d92m", 1).endswith("__k7d92m__0001")


def test_table_names_from_different_scopes_differ() -> None:
    # This is what stops a resumed conversation's stale handle resolving to a
    # live table holding different data.
    mounted = naming.mounted_name("gh", "list_issues")
    assert naming.table_name(mounted, "aaaaaa", 1) != naming.table_name(mounted, "bbbbbb", 1)


def test_sequence_widens_past_9999() -> None:
    mounted = naming.mounted_name("gh", "t")
    assert naming.table_name(mounted, "s", 10000).endswith("__10000")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("plain", '"plain"'), ('we"ird', '"we""ird"'), ("drop table", '"drop table"')],
)
def test_quote_ident(raw: str, expected: str) -> None:
    assert naming.quote_ident(raw) == expected


def test_the_discovered_collision_pair_no_longer_collides() -> None:
    """Regression for a real collision found by brute force against the old
    24-bit tag: `s/a..---_` and `s/a-----_-_` both mounted as `s__a__fa29cc`."""
    assert naming.mounted_name("s", "a..---_") != naming.mounted_name("s", "a-----_-_")


def test_injectivity_check_accepts_distinct_names() -> None:
    mapping = naming.assert_injective([("s", "a"), ("s", "b"), ("t", "a")])
    assert len(mapping) == 3


def test_injectivity_check_fails_loudly_on_a_forced_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wider digest lowers the probability of a collision; it cannot prove
    injectivity. The loud failure is the actual fix, so it is tested by forcing
    the case rather than by hoping one never occurs."""
    monkeypatch.setattr(naming, "tag", lambda value: "constant")
    with pytest.raises(naming.NameCollisionError) as caught:
        naming.assert_injective([("s", "alpha"), ("s", "alpha!")])
    assert "alpha" in str(caught.value)


def test_repeated_identical_pairs_are_not_a_collision() -> None:
    assert len(naming.assert_injective([("s", "a"), ("s", "a")])) == 1
