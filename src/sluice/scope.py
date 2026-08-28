"""Scope derivation (spec 12).

A client may reuse one stdio process across conversations, so process lifetime
is not conversation lifetime. The scope tag embedded in every table name is what
makes two things true:

1. A stale handle from a previous process cannot resolve to a live table holding
   different data. Sequence numbers restart at 1 on every process start, so
   without a scope tag a resumed conversation could query `..._0001` and get a
   clean answer about someone else's result set. This half is unconditional.
2. A conversation can only name tables whose handles it was given, because names
   are unguessable and catalog enumeration is blocked.

The second half is capability-based, not enforced. See spec 12 for the residual
risk.
"""

import hashlib
import secrets

SCOPE_TAG_LENGTH = 32
"""32 hex characters, so 128 bits.

An earlier 8-character tag was 32 bits, which is not capability-token strength.
Scope tags are the only thing standing between one conversation and another's
tables (spec 12), and a table name is long enough already that the agent copies
it from the handle rather than typing it, so the extra characters cost nothing
that matters.
"""

SCOPE_META_KEYS: tuple[str, ...] = (
    "conversationId",
    "conversation_id",
    "sessionId",
    "session_id",
    "threadId",
    "thread_id",
)
"""Request `_meta` keys that may carry a conversation identifier.

None of these is standardized. MCP defines `_meta` as an open extension point
and says nothing about conversation identity, so this is a best-effort probe of
what clients are observed to send. When none is present Sluice mints per call,
which is stricter, not weaker.
"""


def mint() -> str:
    """A fresh unguessable scope tag.

    `secrets`, never `random`: this is a capability token, and a predictable
    sequence would satisfy every "the values are all different" test while
    providing no isolation at all.
    """
    return secrets.token_hex(SCOPE_TAG_LENGTH // 2)


def from_conversation_id(conversation_id: str) -> str:
    """A stable scope tag for a client-supplied conversation identifier.

    Hashed rather than used directly: the identifier may be long, may contain
    characters that are not identifier-safe, and lands in a table name that the
    agent reads.

    BLAKE2, never the builtin `hash()`: PYTHONHASHSEED randomization would make
    the same conversation resolve to different scopes in different processes,
    silently orphaning every table from a resumed conversation.
    """
    digest = hashlib.blake2b(conversation_id.encode("utf-8"), digest_size=16).hexdigest()
    return digest[:SCOPE_TAG_LENGTH]


def derive(meta: object | None) -> tuple[str, bool]:
    """Return `(scope_tag, from_client)`.

    `from_client` is False when the tag was minted, which means scope is
    per-call rather than per-conversation.
    """
    candidate = _conversation_id(meta)
    if candidate is not None:
        return from_conversation_id(candidate), True
    return mint(), False


def _conversation_id(meta: object | None) -> str | None:
    if meta is None:
        return None
    mapping = meta if isinstance(meta, dict) else getattr(meta, "__dict__", None)
    if not isinstance(mapping, dict):
        return None
    for key in SCOPE_META_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None
