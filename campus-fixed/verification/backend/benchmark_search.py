"""Compare search against the supplied frontend archive on a temporary data copy.

Run at repository root with --baseline-zip pointing to the unchanged 1.9
frontend archive. Only its _scan/keys methods are loaded; both variants use the
current Store so this isolates the search optimization from the read-lock fix.
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import statistics
import tempfile
import time
import zipfile
from pathlib import Path

from sqlalchemy import event as sql_event

from server.campus.content import Pipeline
from server.campus.events import Events
from server.campus.platform import Core, Settings
from server.campus.search import Search
from server.campus.zh_index import TABLE, LocalTextIndex, match_expression, narrowable


def baseline_method(archive, suffix, owner, method):
    (name,) = [name for name in archive.namelist() if name.endswith("/" + suffix)]
    tree = ast.parse(archive.read(name).decode("utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method)
    scope = {"TABLE": TABLE, "match_expression": match_expression, "narrowable": narrowable}
    exec(compile(ast.Module(body=[node], type_ignores=[]), name, "exec"), scope)
    return scope[method]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", type=Path, required=True)
    args = parser.parse_args()
    with zipfile.ZipFile(args.baseline_zip) as archive:
        LegacySearch = type(
            "LegacySearch", (Search,), {"_scan": baseline_method(archive, "server/campus/search.py", "Search", "_scan")}
        )
        LegacyIndex = type(
            "LegacyIndex",
            (LocalTextIndex,),
            {"keys": baseline_method(archive, "server/campus/zh_index.py", "LocalTextIndex", "keys")},
        )
    sample = Path("data/campus.sqlite3")
    original_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
    os.environ["CAMPUS_MEILI_URL"] = ""
    results = []
    with tempfile.TemporaryDirectory() as directory:
        shutil.copyfile(sample, Path(directory) / "campus.sqlite3")
        core = Core(Settings(data_dir=Path(directory), secret="synthetic-benchmark-secret-" * 3))
        try:
            core.pipeline, core.events = Pipeline(core), Events(core)
            core.search = Search(core)
            events = core.store.list("events")
            for value in events:
                core.search.index_event({"subject": {"id": value["id"], "revision": value["version"]}})
            legacy = LegacySearch(core)
            legacy.local = LegacyIndex(core.store)
            for query in ["奖学金", "国家奖学金申请材料", "银河计数器", "GPA", "报名"]:
                before = legacy.search(query)
                after = core.search.search(query)
                assert before == after, f"Result changed for {query}"
                row = {"query": query, "response_equal": True, "returned_hits": len(after["hits"])}
                for label, search in [("before", legacy), ("after", core.search)]:
                    elapsed, queries = [], []
                    for _ in range(7):
                        count = 0

                        def count_query(*args):
                            nonlocal count
                            count += 1

                        sql_event.listen(core.store.engine, "after_cursor_execute", count_query)
                        try:
                            start = time.perf_counter()
                            result = search.search(query)
                            elapsed.append((time.perf_counter() - start) * 1000)
                        finally:
                            sql_event.remove(core.store.engine, "after_cursor_execute", count_query)
                        assert result == after
                        queries.append(count)
                    row[label] = {
                        "median_ms": round(statistics.median(elapsed), 3),
                        "sql_statements": statistics.median(queries),
                        "samples": 7,
                    }
                row["speedup"] = round(row["before"]["median_ms"] / row["after"]["median_ms"], 2)
                results.append(row)
            published = sum(value["publication_state"] == "published" for value in events)
        finally:
            core.store.close()
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == original_hash
    print(
        json.dumps(
            {
                "published_notices": published,
                "sample_unchanged": True,
                "database_sha256": original_hash,
                "queries": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
