import json
import sys
from pathlib import Path

BASE = "".join(chr(ord("a") + (i * 7) % 26) for i in range(1000))


def nested_replace(k: int) -> str:
    a = "a" * k
    return f"SELECT sum(length(replace(replace(replace(s, 'a', '{a}'), 'a', '{a}'), 'a', '{a}'))) FROM t"


def concat_chain(levels: int) -> str:
    ctes = [f"c0 AS (SELECT i, s || '{BASE[:799]}' AS s FROM t)"]
    for level in range(1, levels + 1):
        ctes.append(f"c{level} AS (SELECT i, s || s || s || s AS s FROM c{level - 1})")
    return f"WITH {', '.join(ctes)} SELECT sum(length(s)) FROM c{levels}"


def recursive_doubling(n: int) -> str:
    return (
        "WITH RECURSIVE r(n, s) AS (SELECT 0, 'x' UNION ALL SELECT n + 1, s || s FROM r "
        f"WHERE n < {n}) SELECT sum(length(r.s || t.s)) FROM r, t"
    )


def damerau(length: int) -> str:
    reps = length // 1000
    expr = " || ".join(["b0.s"] * reps)
    return (
        f"WITH b0 AS (SELECT '{BASE}' AS s), b AS (SELECT {expr} AS s FROM b0) "
        "SELECT damerau_levenshtein(b.s, reverse(b.s)) FROM b"
    )


def string_agg(width: int) -> str:
    return (
        f"SELECT length(string_agg(x, '')) FROM (SELECT a.s || '{BASE[:width]}' AS x "
        "FROM t a, t b, t c)"
    )


def list_agg() -> str:
    return "SELECT len(list(a.i + b.i + c.i + d.i)) FROM t a, t b, t c, t d"


def regexp_g(k: int) -> str:
    a = "y" * k
    return f"SELECT sum(length(regexp_replace(s || '{BASE}', '', '{a}', 'g'))) FROM t"


def control_group(width: int) -> str:
    return (
        f"SELECT count(*) FROM (SELECT a.s || '{BASE[:width]}' || a.i || '-' || b.i || '-' || c.i AS x "
        "FROM t a, t b, t c GROUP BY x)"
    )


def nested_replace_tiny_literals(depth: int, rows: int) -> str:
    expr = "s"
    for _ in range(depth):
        expr = f"replace({expr}, 'a', 'aaaa')"
    return f"SELECT sum(length({expr})) FROM t WHERE i < {rows}"


def truncated_wrapper(k: int) -> str:
    a = "a" * k
    inner = f"SELECT replace(replace(replace(s, 'a', '{a}'), 'a', '{a}'), 'a', '{a}') AS x FROM t"
    return f"SELECT left(x, 100) AS x FROM ({inner})"


def normal() -> str:
    return "SELECT count(*), sum(i) FROM t"


PROBES: dict[str, str] = {
    "nested_replace": nested_replace(250),
    "concat_chain": concat_chain(7),
    "recursive_doubling": recursive_doubling(23),
    "damerau_levenshtein": damerau(14000),
    "string_agg": string_agg(1000),
    "list_agg": list_agg(),
    "regexp_replace_g": regexp_g(12000),
    "control_group": control_group(500),
    "nested_replace_tiny_literals": nested_replace_tiny_literals(13, 20),
    "truncated_wrapper": truncated_wrapper(250),
    "normal": normal(),
}


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("probes.json")
    target.write_text(json.dumps(PROBES, indent=1) + "\n")
    for name, sql in PROBES.items():
        print(f"{name}: {len(sql.encode())} bytes")


if __name__ == "__main__":
    main()
