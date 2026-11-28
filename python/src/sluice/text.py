def truncate_to_bytes(text: str, limit: int) -> tuple[str, bool]:
    """Cut to a byte budget on a character boundary.

    Slicing UTF-8 at a byte offset produces invalid text and would corrupt the
    handle the agent depends on.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    cut = encoded[:limit]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True
