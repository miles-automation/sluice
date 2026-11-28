import json
import sys
from pathlib import Path

import duckdb

from sluice.gate import QueryRejectedError, check

probes = json.loads(Path(__file__).with_name("probes.json").read_text())
connection = duckdb.connect(":memory:")
connection.execute("CREATE TABLE t AS SELECT i::INTEGER AS i, 'a' AS s FROM range(100) r(i)")
results: dict[str, str] = {}
for name, sql in probes.items():
    try:
        check(sql, connection, {"t"})
        results[name] = "admitted"
    except QueryRejectedError as exc:
        results[name] = f"rejected: {str(exc)[:120]}"
out = {"duckdb": duckdb.__version__, "gate": results}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1) + "\n")
print(json.dumps(out, indent=1))
