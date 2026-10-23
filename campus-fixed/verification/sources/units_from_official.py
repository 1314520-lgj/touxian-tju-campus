"""t28：从官方「学院（部）」页拿到每个单位的官网主机（可复核的枚举第一步）。

方法：
  1) 抓 https://www.tju.edu.cn/xy_b_.htm —— 学校官网的「学院（部）」列表（44 条）；
  2) 列表每项都指向 https://www.tju.edu.cn/info/1044/<id>.htm 的单位介绍页；
  3) 在单位介绍页里找该校属主机的链接（*.tju.edu.cn，排除 www/信息页/系统站）；
  4) 输出 单位名 → 主机 → 证据页，供下一步逐栏实测使用。

    python verification/sources/units_from_official.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

LIST_PAGE = "https://www.tju.edu.cn/xy_b_.htm"
DETAIL = re.compile(r"^https://www\.tju\.edu\.cn/info/1044/\d+\.htm$")
SKIP_HOSTS = {
    "www.tju.edu.cn",
    "e.tju.edu.cn",
    "mail.tju.edu.cn",
    "vpn.tju.edu.cn",
    "faculty.tju.edu.cn",
    "vr.tju.edu.cn",
    # 每个介绍页的页头/页脚都挂着这些站点级导航，不是单位官网
    "alumni.tju.edu.cn",
    "en.tju.edu.cn",
    "es.tju.edu.cn",
    "fr.tju.edu.cn",
    "ja.tju.edu.cn",
    "ko.tju.edu.cn",
    "de.tju.edu.cn",
    "ru.tju.edu.cn",
    "news.tju.edu.cn",
    "gs.tju.edu.cn",
    "oaa.tju.edu.cn",
    "sie.tju.edu.cn",
    "sdce.tju.edu.cn",
    "xxgkw.tju.edu.cn",
    "home.lib.tju.edu.cn",
    "www.lib.tju.edu.cn",
    "130.tju.edu.cn",
}


def main() -> int:
    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body

    base = tuple(gateway_origins_from_config() or ())
    fetcher = SafeFetcher(trusted_origins=base + ("https://www.tju.edu.cn",))
    requests = {"n": 0}

    def get(url):
        fetcher.trusted_origins = base + ("https://" + (urlsplit(url).hostname or ""),)
        requests["n"] += 1
        result = fetcher.fetch(url)
        return BeautifulSoup(decode_body(result.body, result.content_type), "html.parser")

    soup = get(LIST_PAGE)
    units = []
    for link in soup.select("a[href]"):
        target = urljoin(LIST_PAGE, link.get("href") or "")
        if DETAIL.match(target):
            name = link.get_text(" ", strip=True)
            if name and (name, target) not in {(u["name"], u["detail"]) for u in units}:
                units.append({"name": name, "detail": target})
    print(f"官方「学院（部）」列表：{len(units)} 个单位")

    table = []
    for unit in units:
        row = {"name": unit["name"], "detail": unit["detail"], "host": None, "evidence": None}
        try:
            page = get(unit["detail"])
        except Exception as error:  # noqa: BLE001
            row["error"] = getattr(error, "code", str(error))
            table.append(row)
            continue
        for link in page.select("a[href]"):
            target = urljoin(unit["detail"], link.get("href") or "")
            host = urlsplit(target).hostname or ""
            if host.endswith("tju.edu.cn") and host not in SKIP_HOSTS:
                row.update(host=host, evidence=target)
                break
        table.append(row)
        flag = row["host"] or row.get("error") or "（介绍页里没有校属主机链接）"
        print(f"  {unit['name'][:22]:<24} {flag}")

    out = REPO / "verification" / "sources" / "unit-hosts.json"
    out.write_text(
        json.dumps({"list_page": LIST_PAGE, "units": table, "requests": requests["n"]}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    hosts = sorted({row["host"] for row in table if row["host"]})
    print(f"\n得到主机 {len(hosts)} 个（单位 {len(table)} 个）· 请求 {requests['n']} 次 · 已写 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())