import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memsample import self_sample  # noqa: E402
from worker_py import open_store  # noqa: E402

probe = sys.argv[1]
sql = json.loads(Path(__file__).with_name("probes.json").read_text())[probe]
connection = open_store("256MB", [])
cursor = connection.cursor()
cursor.execute(sql).fetchall()
cursor.close()
rows = connection.execute(
    "SELECT tag, memory_usage_bytes FROM duckdb_memory() WHERE memory_usage_bytes > 0"
).fetchall()
settings = connection.execute(
    "SELECT name, value FROM duckdb_settings() WHERE name IN ('block_allocator_memory', 'allocator_flush_threshold')"
).fetchall()
print(json.dumps({"probe": probe, "rss_mb": round(self_sample()[0] / 2**20),
                  "duckdb_memory": [(t, round(b / 2**20)) for t, b in rows], "settings": settings}))
