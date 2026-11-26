import json
import sys
import time
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
after_query = round(self_sample()[0] / 2**20)
connection.close()
time.sleep(0.5)
after_close = round(self_sample()[0] / 2**20)
reopened = open_store("256MB", [])
reopened.execute("SELECT count(*) FROM t").fetchall()
print(json.dumps({"probe": probe, "rss_after_query_mb": after_query, "rss_after_close_mb": after_close,
                  "rss_after_reopen_mb": round(self_sample()[0] / 2**20)}))
