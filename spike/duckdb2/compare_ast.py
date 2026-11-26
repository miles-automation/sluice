"""Compare AST dumps: python 1.5.5 vs node 2.x. Usage: compare_ast.py old.json new.json"""

import json
import pathlib
import sys


def keys(node, out):
    if isinstance(node, dict):
        kind = node.get("type") if isinstance(node.get("type"), str) else ""
        for key, value in node.items():
            out.add(f"{kind}.{key}")
            keys(value, out)
    elif isinstance(node, list):
        for item in node:
            keys(item, out)
    return out


def kinds(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("type"), str):
            out.add(node["type"])
        for value in node.values():
            kinds(value, out)
    elif isinstance(node, list):
        for item in node:
            kinds(item, out)
    return out


old = json.loads(pathlib.Path(sys.argv[1]).read_text())
new = json.loads(pathlib.Path(sys.argv[2]).read_text())
print("engines", old["engine"], new["engine"])
old_keys, new_keys, old_kinds, new_kinds = set(), set(), set(), set()
same = 0
changed = []
for sql, before in old["statements"].items():
    after = new["statements"][sql]
    keys(before["ast"], old_keys)
    keys(after["ast"], new_keys)
    kinds(before["ast"], old_kinds)
    kinds(after["ast"], new_kinds)
    if json.dumps(before["ast"], sort_keys=True) == json.dumps(after["ast"], sort_keys=True):
        same += 1
    else:
        changed.append(sql)
print(f"identical AST: {same} of {len(old['statements'])}")
print("keys removed:", sorted(old_keys - new_keys))
print("keys added:", sorted(new_keys - old_keys))
print("node types removed:", sorted(old_kinds - new_kinds))
print("node types added:", sorted(new_kinds - old_kinds))
print("changed statements:")
for sql in changed:
    print("  ", sql)
print("outcome differences (statement type or serialize error):")
for sql, before in old["statements"].items():
    after = new["statements"][sql]
    a, b = before["ast"], after["ast"]
    if (
        before["types"] != after["types"]
        or a.get("error") != b.get("error")
        or a.get("raised") != b.get("raised")
    ):
        print("  ", repr(sql))
        print("     types", before["types"], "->", after["types"])
        print(
            "     error",
            a.get("error"),
            str(a.get("error_message"))[:70],
            "->",
            b.get("error"),
            str(b.get("error_message"))[:70],
        )
