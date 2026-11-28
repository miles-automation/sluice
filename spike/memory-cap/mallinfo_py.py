import ctypes
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memsample import self_sample  # noqa: E402
from worker_py import open_store  # noqa: E402

FIELDS = ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")


class MallInfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in FIELDS]


libc = ctypes.CDLL("libc.so.6")
libc.mallinfo2.restype = MallInfo2


def snap(label: str) -> dict[str, object]:
    info = libc.mallinfo2()
    return {"at": label, "rss_mb": round(self_sample()[0] / 2**20),
            "heap_arena_mb": round(info.arena / 2**20), "mmapped_mb": round(info.hblkhd / 2**20),
            "in_use_mb": round(info.uordblks / 2**20), "free_in_heap_mb": round(info.fordblks / 2**20)}


probe = sys.argv[1]
sql = json.loads(Path(__file__).with_name("probes.json").read_text())[probe]
connection = open_store("256MB", sys.argv[2:])
out = [snap("setup")]
cursor = connection.cursor()
cursor.execute(sql).fetchall()
cursor.close()
out.append(snap("after query"))
libc.malloc_trim(0)
out.append(snap("after malloc_trim"))
connection.close()
out.append(snap("after connection.close"))
print(json.dumps({"probe": probe, "snapshots": out}))
