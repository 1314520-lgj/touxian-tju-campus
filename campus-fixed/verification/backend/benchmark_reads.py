"""Synthetic SQLite reader latency while a writer deliberately holds 150 ms.

Run from the repository root: python -m verification.backend.benchmark_reads
This measures contention, not application throughput or a production SLA.
"""

import copy
import json
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server.campus.db import Store


def legacy_get(store, collection, key):
    # Exact standalone read behavior from the imported 1.9 frontend baseline.
    with store.transaction():
        row = store._row(collection, key, lock=True)
        return copy.deepcopy(row["data"]) if row else None


def main():
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        url = f"sqlite:///{Path(directory) / 'benchmark.sqlite3'}"
        writer, reader = Store(url), Store(url)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                for label, read in [("before", legacy_get), ("after", Store.get)]:
                    elapsed = []
                    for _ in range(9):
                        writer.put("fixture", "a", {"version": 1})
                        ready = threading.Event()

                        def hold_write():
                            with writer.transaction():
                                writer.put("fixture", "a", {"version": 2})
                                ready.set()
                                time.sleep(0.15)

                        future = pool.submit(hold_write)
                        assert ready.wait(timeout=5)
                        started = time.perf_counter()
                        result = read(reader, "fixture", "a")
                        elapsed.append((time.perf_counter() - started) * 1000)
                        future.result(timeout=5)
                        assert result == {"version": 2 if label == "before" else 1}
                    results[label] = {
                        "median_ms": round(statistics.median(elapsed), 3),
                        "min_ms": round(min(elapsed), 3),
                        "max_ms": round(max(elapsed), 3),
                        "samples": len(elapsed),
                    }
        finally:
            reader.close()
            writer.close()
    print(json.dumps({"writer_hold_ms": 150, "read_latency": results}, indent=2))


if __name__ == "__main__":
    main()
