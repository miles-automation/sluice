"""Dump json_serialize_sql for statements.json with the Python DuckDB in use.

Run with a throwaway environment, for example:
    uv run --no-project --python 3.14 --with duckdb==1.5.5 python ast_dump_python.py ast-1.5.5.json
"""

import json
import pathlib
import sys

import duckdb

here = pathlib.Path(__file__).parent
statements = json.loads((here / "statements.json").read_text())
connection = duckdb.connect()
out: dict[str, object] = {"engine": duckdb.__version__, "statements": {}}
for sql in statements:
    try:
        raw = connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0]
        document: object = json.loads(raw)
    except duckdb.Error as exc:
        document = {"raised": type(exc).__name__}
    try:
        extracted = connection.extract_statements(sql)
        typed: object = [s.type.name for s in extracted]
    except duckdb.Error as exc:
        typed = {"raised": type(exc).__name__}
    out["statements"][sql] = {"types": typed, "ast": document}  # type: ignore[index]
pathlib.Path(sys.argv[1]).write_text(json.dumps(out, indent=1, sort_keys=True))
