import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memsample import release_allocator, self_sample  # noqa: E402
from worker_py import open_store  # noqa: E402

probe = sys.argv[1]
sql = json.loads(Path(__file__).with_name("probes.json").read_text())[probe]
connection = open_store("256MB", [])
after: list[int] = []
for _ in range(4):
    cursor = connection.cursor()
    cursor.execute(sql).fetchall()
    cursor.close()
    after.append(round(self_sample()[0] / 2**20))
time.sleep(1)
release = release_allocator()
print(json.dumps({"lang": "python", "probe": probe, "rss_after_each_mb": after,
                  "release": release, "rss_after_release_mb": round(self_sample()[0] / 2**20)}))
