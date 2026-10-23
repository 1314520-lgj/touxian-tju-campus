# t28：天津大学官网通知栏目清点与注册（2026-10-21 实测）

## 结论（一句话）

用官方「学院（部）」页把 TJU 官网体系里的**69 个主机**、**173 个栏目候选**逐个真抓了一遍：
**活跃 17 个 / 低频 6 个 / 已废弃 54 个 / 该路径不存在 96 个**；
其中**6 个活跃栏目**（卡片能显示真实通知标题）已注册为官方来源，内置来源 24 → **30**。
在一份 `data-operator` 副本上跑一次真实日常更新：公开事项 **122 → 125（新增可见 +3）**，
6 个新来源共采到 **58 份快照**（真实标题与原始 URL 见第 4 节）。
同时**明确没有**注册 5 个看着活跃但卡片会显示错标题的栏目（第 5 节）——宁少勿滥。

## 1. 枚举方法（可照着重跑）

```bash
python verification/sources/units_from_official.py   # ① 官方学院（部）页 → 每个单位的官网主机
python verification/sources/site_inventory.py --max-hosts 80 \
    --hosts-file verification/sources/unit-hosts.json --titles   # ② 逐主机逐栏目实测 + 分档
python verification/sources/title_check.py            # ③ 活跃栏目：会入队什么 + 文章标题检查
python verification/sources/siteinv_numbers.py <campus.sqlite3> [标签]   # ④ 对照数字（只读）
```

- **① 单位与主机**：抓 `https://www.tju.edu.cn/xy_b_.htm`（学校官网的「学院（部）」列表，
  36 个单位），再抓每个单位的介绍页 `https://www.tju.edu.cn/info/1044/NNN.htm`，
  取页内学校属主机链接 → **35/36 拿到官网主机**（国际教育学院那页没有链接，其主机 `sie.tju.edu.cn`
  由官网导航补上）。名字与主机对照见 `unit-hosts.json`。
- **② 主机池**：上述 35 个 + 已注册来源的 16 个主机 + `www.tju.edu.cn` 首页里发现的 24 个
  = **69 个候选主机**（去重后）。
- **③ 栏目候选**：每个主机抓一次首页，取「文字含通知/公告/公示且路径不像文章页」的链接
  （上限 4 条）；若首页没有这种链接，再试 3 条规律路径（`/index/tzgg.htm`、`/xwzx/tzgg.htm`、`/tzgg.htm`）
  → **173 个栏目候选**，逐个用产品自己的 `SafeFetcher` 真抓一次（受信网关为 `config/` 声明的官方 origin）。
- **④ 记录字段**：官方 URL、所属单位、栏目名、可达性、列表条数、**最近 5 条日期**、
  以及活跃档的「文章页第一个 `<h1>`」（产品取标题的规则）。

> 全部原始记录：`verification/sources/site-inventory.json`（173 条候选 + 69 行主机表）、
> `unit-hosts.json`（36 个单位 → 主机）、`title-check.json`（23 个活跃/低频栏目的入队与标题实测）。

## 2. 分档（实读 site-inventory.json）

| 档位 | 判据 | 数量 |
|---|---|---|
| **活跃** | 列表最新条目 ≤30 天 | **17** |
| **低频** | ≤90 天 | **6** |
| **已废弃** | >90 天，或页面有内容但没有文章列表结构 | **54** |
| **该路径不存在** | 抓取失败（`upstream_http_404` 96 条、`fetch_transport_failed` 1 条） | **97** |

活跃档 17 个（URL · 列表条数 · 最新日期）：

```
come.tju.edu.cn/xwzx/tzgg.htm        42 · 2026-10-16   （已注册）
cs.tju.edu.cn/xwgg/notices.htm       10 · 2026-10-15   （标题不合格，未注册）
cse.tju.edu.cn/index/tzgg.htm        15 · 2026-10-20   （已注册）
cse.tju.edu.cn/rczp/zpgg.htm          2 · 2026-10-20   （标题不合格，未注册）
gs.tju.edu.cn/xwydsgz/xwtz.htm       12 · 2026-10-21   （与已注册的学位通知同内容，未注册）
ico.tju.edu.cn/hdyg/xsxmtz.htm       15 · 2026-10-12   （已注册）
ico.tju.edu.cn/hdyg/gsgg.htm         15 · 2026-09-29   （公示类，未注册）
marxism.tju.edu.cn/tzgg/tzgg.htm     13 · 2026-10-11   ★本轮注册
me.tju.edu.cn/xwzx/tzgg.htm          58 · 2026-10-15   （已注册）
mstu.tju.edu.cn/index/tztg.htm       16 · 2026-10-19   ★本轮注册
scet.tju.edu.cn/xwzx/tzgg.htm        25 · 2026-10-13   （标题不合格，未注册）
science.tju.edu.cn/index/tzgg.htm    10 · 2026-10-19   （已注册）
sdem.tju.edu.cn/tzgg.htm             24 · 2026-09-28   ★本轮注册
spst.tju.edu.cn/xydt/tzgg.htm        13 · 2026-10-21   ★本轮注册
synbm.tju.edu.cn/xwzx/tzgg.htm       19 · 2026-09-29   ★本轮注册
tjusee.tju.edu.cn/xwzx/tzgg.htm      17 · 2026-10-20   （已注册）
tjusfl.tju.edu.cn/tzgg.htm            5 · 2026-10-10   ★本轮注册
```

低频档 6 个：`qiushi.tju.edu.cn/tzgg.htm`（2026-09-09）、`rwys.tju.edu.cn/xydt/tztg.htm`（09-14）、
`rwys.tju.edu.cn/gdpx/xxgg.htm`（09-05）、`smxy.tju.edu.cn/xwzx/tzgg.htm`（09-16）、
`sdce.tju.edu.cn/wljy/wltzgg.htm`（08-26，1 条）、`oaa.tju.edu.cn/bszy/tzgg.htm`（09-14，
该栏目的原生条目本来就不多，公众号链接另有来源覆盖）。

## 3. 本轮注册的 6 个来源（`config/` 声明 + `builtin_sources()` 钉住 origin）

| 来源名 | origin_url | 列表条数 | 最新一条 | 独立标题实测 |
|---|---|---|---|---|
| 天津大学马克思主义学院 · 通知公告 | `https://marxism.tju.edu.cn/tzgg/tzgg.htm` | 13 | 2026-10-11 | `<h1>`「10月12日国外马克思主义研究方向硕士研究生答辩公告」 |
| 天津大学医学院 · 通知通告 | `https://mstu.tju.edu.cn/index/tztg.htm` | 16 | 2026-10-19 | `<h1>`「医学院关于开展2027年硕博连读研究生选拔的通知」 |
| 天津大学药物科学与技术学院 · 通知公告 | `https://spst.tju.edu.cn/xydt/tzgg.htm` | 13 | 2026-10-21 | `<h1>`「天津大学药物科学与技术学院2027年博士研究生招生办法」 |
| 天津大学外国语学院 · 通知公告 | `https://tjusfl.tju.edu.cn/tzgg.htm` | 5 | 2026-10-10 | `<h1>`「2026年天津大学英语翻译（笔译）比赛获奖情况公布」 |
| 天津大学卫生应急学院 · 通知公告 | `https://sdem.tju.edu.cn/tzgg.htm` | 24 | 2026-09-28 | `<h1>`「天津大学卫生应急学院2027级推免生拟录取名单公示」 |
| 天津大学合成生物与生物制造学院 · 通知公告 | `https://synbm.tju.edu.cn/xwzx/tzgg.htm` | 19 | 2026-09-29 | 文章页没有 `<h1>`，但列表链接文字本身就是干净标题（产品会退回列表标题） |

来源 id 用仓库既有规则生成：`uuid5(NAMESPACE_URL, "tju-campus-source:" + origin_url)`
（`test_source_catalog` 会核这一点；一开始用了随机 uuid4，被测试挡下后改正）。
一个 origin 一个来源的不变量保持不变：`builtin_sources()` 30 个、origin 唯一 ✓、id 唯一 ✓。

## 4. 实测采到的真实条目（副本上跑完一次日常更新后，来自 6 个新来源）

```
https://tjusfl.tju.edu.cn/info/1133/9122.htm  «天津大学外国语学院2026年招聘公告！»
https://sdem.tju.edu.cn/info/1104/8260.htm    «卫生应急学院2026年专业型博士研究生招生综合考核安排»
https://synbm.tju.edu.cn/info/1271/5215.htm   «根据学校招生工作要求，经考核，现将我院2026年专业学位博士研究生拟录取名单进行公示，公示期»
https://spst.tju.edu.cn/info/1018/12052.htm   «药学院2026年普通招考类专业型博士研究生拟录取公示名单（第二批次）(已结束）»
https://marxism.tju.edu.cn/system/_content/download.jsp?…  «第十届21世纪世界马克思主义论坛邀请函网络版.docx»（附件）
```

6 个新来源共 **58 份快照**；公开（active）事项里来自新来源的有 2 条：
«天津大学药物科学与技术学院2027年博士研究生招生办法»、«会议通知»。

> **要说的实话**：其中「会议通知」这个标题太泛（列表文字本身就很短），卡片上看不出是哪场会议。
> 这不是注册错了来源，而是该栏目这一条的标题质量差；复核策略没有自动接受它之外的低质标题，
> 但值得作为下一轮的标题质量项跟进（`content.py` 不在本任务范围）。

## 5. 没有注册的栏目与理由（宁少勿滥）

| 栏目 | 为什么不注册 |
|---|---|
| `scet.tju.edu.cn/xwzx/tzgg.htm`（化工学院通知公告，活跃） | 列表链接文字是「日期 + 栏目标题 + 次数」拼起来的脏串（实测：`2026-10-13 天津大学化工学院大型仪器测试平台2026年下半年培训和讲座通知 13 2026-10 …`），文章页没有 `<h1>` → 卡片会显示脏串 |
| `cs.tju.edu.cn/xwgg/notices.htm`（计算机学院通知公告，活跃） | 文章页第一个 `<h1>` 是「计算机科学与技术学院」（学院名，不是通知标题） |
| `cse.tju.edu.cn/rczp/zpgg.htm`（网络安全学院招聘公告，活跃但 2 条） | `<h1>` 是「人才招聘」；同类内容已由该学院的「通知公告」来源覆盖 |
| `gs.tju.edu.cn/xwydsgz/xwtz.htm`（研究生院学位通知，活跃） | 与已注册的 `gs.tju.edu.cn/tzgg/xwtz.htm` 是同一批通知的另一入口，注册会造成重复候选 |
| `ico.tju.edu.cn/hdyg/gsgg.htm`（国际处公示公告，活跃） | 内容是名单/结果公示，学生可办性低；同类已由国际处学生项目通知覆盖 |
| 低频档 3 个（未来技术、人文艺术、生命科学） | 最新条目 40–75 天前，注册进来每天消耗请求、短期几乎没有增量 |
| 语言/系统/服务站点（en/fr/ja/ko/ru/es/de、mail、vpn、e、faculty、vr、130、zhuanti…） | 不是通知栏目 |
| `yzb.tju.edu.cn`（研究生招生网） | 第 24 轮已记录：文章页没有独立标题，暂不注册（本轮复核结论不变） |

## 6. 对照数字（`data-operator` 的副本上实跑，**区分新增可见与版本上升**）

| 指标 | 注册前 | 注册后（一次 `daily_update --apply`） |
|---|---|---|
| 来源注册数 | 22 | **32**（新增 10：本轮 6 个 + config 里此前已声明但该副本未注册的 4 个） |
| 内置来源（`builtin_sources()`） | 24 | **30** |
| 事项总数 | 201 | 207 |
| **公开（active）事项** | **122** | **125** → **新增可见 +3 条** |
| 快照 | 549 | 687（新来源贡献 58） |
| 该次运行的复核 | — | 接受 11 · 拒绝 80 · 留给人 176 · 已合并 30 |
| 该次运行的发布 | — | 发布 12 条（其中新增可见 3 条；其余是既有事项的重新发布） |
| 下一次（增量）运行 | — | 退出码 0：新增快照 126 · 新候选 92 · 接受 1 · 拒绝 45 · 留给人 148 · 发布 1 |

**新增可见 ≠ 版本上升**：公开列表 122 → 125 是学生真的多看到 3 条；
该次运行「发布 12 条」里有 9 条是既有事项在版本升级后的重新发布，
所以报告里的「新增可见」一律以**公开事项数**为准，不引用「发布条数」。

契约命令 1（全新数据目录）：`来源注册 新增 30 · 复用 0` → 30 个来源
`成功 15 · 降级 13 · 需授权 2 · 失败 0`，新增快照 275 · 新候选 186 · 解析 ok 115，
第二次增量运行退出码 0（干净）。

## 7. 红线核对

- **不绕过反爬**：本轮 0 次公众号抓取尝试，也没有任何验证码处理；公众号链接仍然一律拒绝
  （跑完的日志里仍是 `article_redirected_off_pattern`）。
- **不伪造身份**：所有请求都走产品自己的 `SafeFetcher`，没有自定义 UA/cookie。
- **不放松地址安全判定**：`server/campus/security.py`、`content.py` 未动；TUN fake-IP 场景一律靠
  `config/` 里声明的官方 origin 走显式受信网关（探测脚本同样只在单次调用里显式声明该 origin）。
- **宁少勿滥**：5 个活跃但卡片会显示错标题的栏目明确不注册（第 5 节），低频档也压着没注册。

## 8. 未验证 / 限制

1. **只扫到「学院（部）」列表页覆盖的单位**（36 个）。机关部处（如人事处、学工部、保卫处）与
   直属单位没有出现在该页上，本轮**没有**穷举；下一轮可从 `https://www.tju.edu.cn/tdgk/jgsz.htm`
   （机构设置）继续，或用各站点首页的通知链接补扫。
2. 每个主机只抓了首页 + 最多 4 条通知链接 + 3 条规律路径，**不保证**找到该主机的全部栏目。
3. 分档只用**列表里能解析出的日期**：`job.tju.edu.cn` 这类把日期写在别处的站点会被低估
   （它已注册，实际内容更活跃）。
4. `--titles` 的独立标题检查是按「第一个非空 `<h1>`」判断的（与产品一致），
   但只看了每个栏目的前 1–3 条；栏目里个别条目可能有不同的模板。
5. 新注册 6 个源的 **日增量**没有单独跟踪；本轮只看到一次运行的快照与公开增量。
6. 注册后的第一次运行里有 13 个来源处于「设计内降级」（附件受验证码/`unsafe_address` 限制），
   其中新来源 `mstu`、`sdem`、`synbm` 各有 1 条附件受限——文章本身已采到，附件不绕过。

## 9. 复现命令

```bash
python verification/sources/units_from_official.py
python verification/sources/site_inventory.py --max-hosts 80 --hosts-file verification/sources/unit-hosts.json --titles
python verification/sources/title_check.py
python verification/sources/show_hosts.py
python verification/sources/siteinv_numbers.py <campus.sqlite3> [标签]
```