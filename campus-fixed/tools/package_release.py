#!/usr/bin/env python3
"""Package the campus release tree into the deliverable ZIP, then verify it.

    python tools/package_release.py --root . --out ../偷闲天大版-1.9-xxx.zip

The archive holds exactly the files RELEASE_MANIFEST.json lists, plus the
manifest itself, under a single top-level folder named after the release tree.
Nothing is discovered by walking the directory, so a stray cache directory or a
half-checkpointed SQLite journal cannot ride along: if a file is in the archive
it is in the manifest, and vice versa.

Immediately after writing, the archive is handed back to
`tools.build_release_manifest.verify`, which re-reads every entry and checks its
SHA-256, byte length and ZIP CRC against the manifest. A truncated, re-packed or
hand-edited archive fails here rather than in the reviewer's hands. A
`<archive>.sha256` sidecar is written in `sha256sum` format.

The archive is content-verified, not byte-reproducible: entry timestamps come
from the files on disk, so packing the same tree twice can differ in bytes while
both archives verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_release_manifest import MANIFEST_NAME, verify


def package(root: Path, out: Path) -> int:
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    listed = [entry["path"] for entry in manifest["files"]]
    folder = root.name

    members: list[tuple[str, Path]] = []
    for relative in listed:
        source = root / relative
        if not source.is_file():
            print(f"FAIL: manifest lists {relative}, which is not on disk", file=sys.stderr)
            return 1
        members.append((f"{folder}/{relative}", source))
    members.append((f"{folder}/{MANIFEST_NAME}", root / MANIFEST_NAME))
    members.sort(key=lambda item: item[0])

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for arcname, source in members:
            info = zipfile.ZipInfo.from_file(source, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            # from_file() sets external_attr to the host's mode; normalise the
            # permission bits so a checkout with a different umask does not
            # change the archive.
            info.external_attr = 0o644 << 16
            with bundle.open(info, "w") as handle:
                handle.write(source.read_bytes())

    digest = hashlib.sha256(out.read_bytes()).hexdigest()

    status = verify(root, out)
    if status != 0:
        print("FAIL: the archive does not match the manifest it carries", file=sys.stderr)
        return status

    # Written only after the archive verifies, so a sidecar never vouches for
    # an archive that failed its own check.
    sidecar = out.with_name(out.name + ".sha256")
    sidecar.write_text(f"{digest}  {out.name}\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.name}: {len(members)} files, sha256 {digest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", type=Path, help="release tree root (default: current directory)")
    parser.add_argument("--out", required=True, type=Path, help="path of the ZIP to write")
    args = parser.parse_args()
    return package(args.root.resolve(), args.out.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
