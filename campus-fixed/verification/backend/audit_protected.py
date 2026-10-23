"""Compare protected deliverable bytes with the supplied source archive."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from tools.build_release_manifest import iter_shipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    with zipfile.ZipFile(args.baseline_zip) as archive:
        original = {name.partition("/")[2]: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    current = dict(iter_shipped(root))
    groups = {
        "contracts": lambda name: name.startswith("campus-contracts/"),
        "frontend_source": lambda name: name.startswith("src/"),
        "dist": lambda name: name.startswith("dist/"),
        "web": lambda name: name.startswith("web/"),
        "public_sample_and_snapshots": lambda name: name.startswith("data/"),
        "deployment": lambda name: name.startswith("deploy/"),
        "dependency_manifests": lambda name: (
            name.startswith("requirements") or name in {"pnpm-lock.yaml", "package.json", "config/campus-sources.json"}
        ),
    }
    results = {}
    for group, includes in groups.items():
        before = {name for name in original if includes(name)}
        after = {name for name in current if includes(name)}
        changed = sorted(name for name in before & after if original[name] != current[name].read_bytes())
        assert before == after and not changed, (group, before ^ after, changed)
        results[group] = {"files": len(before), "changed": 0, "added": 0, "deleted": 0}
    dist = {name.removeprefix("dist/"): path.read_bytes() for name, path in current.items() if name.startswith("dist/")}
    web = {name.removeprefix("web/"): path.read_bytes() for name, path in current.items() if name.startswith("web/")}
    assert dist == web
    protected_runtime = [
        name for name in current if Path(name).name == ".server-secret" or name.endswith(("-wal", "-shm"))
    ]
    assert not protected_runtime, "Runtime-only files must not be packaged"
    print(
        json.dumps(
            {
                "baseline_zip_sha256": hashlib.sha256(args.baseline_zip.read_bytes()).hexdigest(),
                "groups": results,
                "dist_web_byte_identical": True,
                "runtime_secret_or_sidecar_files_in_release": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
