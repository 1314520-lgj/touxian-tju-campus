"""t28：把 site-inventory.json 的每主机表打印出来（谁活着、露出哪些通知栏目）。"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
data = json.loads((REPO / "verification" / "sources" / "site-inventory.json").read_text(encoding="utf-8"))
hosts = data["host_table"]
known = set(data["known_hosts"])
print(f"主机 {len(hosts)} 个（已注册 {sum(1 for host in hosts if host in known)} 个）\n")
for host, row in sorted(hosts.items()):
    flag = "OK " if row["reachable"] else "ERR"
    title = (row.get("homepage_title") or row.get("error") or "")[:40]
    columns = row.get("column_links") or []
    print(f"{flag} {'[已注册]' if host in known else '        '} {host:<30} {title:<42} 栏目链接 {len(columns)}")
    for link in columns[:4]:
        print(f"        · {link}")