"""把「当前源码树」按仓库自己的发布清单规则导出成一个等价发布包目录。

为什么需要它：随包分发的 `campus-fixed-realuse.zip`（2026-10-20 14:59）早于
t9（scripts/daily_update.py）、t10（tools/install_daily_task.ps1）、t11（dist 重建），
冷启动验收里有两条（触发日常更新、坏来源）在 zip 里根本跑不了。这里不重新打包、
也不改 RELEASE_MANIFEST.json，只是用 `tools.build_release_manifest.iter_shipped`
——也就是打包脚本使用的同一份「哪些文件该出厂」定义——把当前源码树复制到全新目录，
得到一个等价于「今天重新打包」的干净树，用来跑同一套冷启动步骤。

用法：
    python verification/coldstart-t13/build_equivalent_package.py <目标目录>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.build_release_manifest import iter_shipped  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    target = Path(argv[1]).resolve()
    tree = target / "campus-fixed"
    if target.exists():
        shutil.rmtree(target)
    copied = 0
    skipped = []
    for relative, source in iter_shipped(REPO):
        destination = tree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            skipped.append(relative)
            continue
        shutil.copy2(source, destination)
        copied += 1
    print(f"copied {copied} files into {tree}")
    print(f"skipped non-file entries: {skipped[:5]}")
    for name in ("scripts/daily_update.py", "tools/install_daily_task.ps1", "dist/campus.html", "RELEASE_MANIFEST.json"):
        print(f"  {name}: {(tree / name).is_file()}")
    print(f"  data-operator present: {(tree / 'data-operator').exists()}  (must be False: cold start)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))