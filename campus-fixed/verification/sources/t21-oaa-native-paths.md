# t21：五个教务处栏目的原生通路（2026-10-21 现场抓取 + 副本实测）

> 本目录同时存放 t18 的报告（`README.md`）；本文件是 t21 的交付物，不覆盖它。

## 结论（一句话）

「五个栏目只列公众号、同站 0 条」**只对两个栏目成立**：学生竞赛、竞赛科创通知（含它们的分页页）
确实 0 条同站文章；另外三个栏目（通知公告、学业指导、通知公告总览）**一直有同站原生文章**
（2/3/1 条），它们进不了队列是**抓取口径与判失败口径**的问题，不是没有通路。
修好之后这三个栏目的原生文章真正采到了（completed 0→2 / 0→3 / 0→1），
两个纯公众号栏目的合规替身（新工科教育创新中心，第 14 轮注册）实测 **10/10** 采到。
公众号反爬页仍然一律拒绝，SafeFetcher 与地址安全判定一行未改。

## 1. 逐栏目结论（现场抓取，2026-10-21）

抓取方式：产品自己的 `SafeFetcher` + `feed_items`（受信网关来自 `config/campus-sources.json`，
等价 `--gateway-from-config`），脚本 `scan_columns.py` / `discover_native.py`，
原始清单 `columns-scan.json`（含每个页面**全部链接**逐条记录）。

| 栏目 | 栏目页 | 实际入队 | 同站原生 | 公众号 | 结论 |
|---|---|---|---|---|---|
| 教务处 · 通知公告 | `oaa.tju.edu.cn/bszy/tzgg.htm` | 20 | **2** | 18 | 有原生通路（同页） |
| 教务处 · 学业指导 | `oaa.tju.edu.cn/tzgg/xyzd.htm` | 20 | **3** | 17 | 有原生通路（同页） |
| 教务处 · 通知公告总览 | `oaa.tju.edu.cn/tzgg.htm` | 15 | **1** | 14 | 有原生通路（同页） |
| 教务处 · 学生竞赛 | `oaa.tju.edu.cn/cxsj/xsjs.htm` | 6 | 0 | 6 | 无原生通路 → 需替身 |
| 教务处 · 竞赛科创通知 | `oaa.tju.edu.cn/tzgg/js_kc.htm` | 20 | 0 | 20 | 无原生通路 → 需替身 |

现场看到的同站原生文章（原文 URL + 列表标题）：

- 通知公告：`https://oaa.tju.edu.cn/info/1059/9030.htm`「2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会通知——第二批」、`…/9000.htm`「《国际化拔尖创新人才培养计划》课程选课通知」
- 学业指导：`https://oaa.tju.edu.cn/info/1056/8760.htm`「天津大学关于启动2024级辅修专业报名工作的通知」、`…/8700.htm`、`…/8440.htm`
- 通知公告总览：`https://oaa.tju.edu.cn/info/1059/9030.htm`（同一条，两个栏目都列）

**为什么 t19 报的是「同站 0 条」**：`feed_items(result, adapter, config)` 不传 `limit` 时默认
`MAX_DOCUMENTS=8`，而代码是**先按文档顺序收集到 8 条、再按「可读优先」排序**——
公众号链接在前、原生文章排在文档后段，于是被截掉了，返回的 8 条全是公众号。
t19 的探针正是按默认 limit 调的（`verification/redirect/dropped_native.py`），
所以量出「每页 8 条、同站 0 条」。采集主链路传的是 `DISCOVERY_LIMIT + 1`（100+1），
**没有**这个截断，所以原生文章其实一直在 backlog 里（这也解释了它们为什么出现在
t19 的 backlog 明细里，且错误码是 `attachment_access_restricted` 而不是重定向）。

### 两个真的没有原生通路的栏目

- 学生竞赛（`cxsj/xsjs.htm`）：6 条全是 `mp.weixin.qq.com/s/…`；相邻/分页页
  `tzgg/js_kc/1..4.htm`、`tzgg/jz_hd.htm`、`tzgg/jsfz.htm` 现场抓取同样 0 条同站文章
  （见 `columns-scan.json` 的 `pages` 数组，逐页记录入队数与域名分布）。
- 竞赛科创通知（`tzgg/js_kc.htm`）：20 条全是公众号，四个分页页 0 条同站文章。

同类通知的**原生替身**：新工科教育创新中心（秘书处设在教务处）`eee.tju.edu.cn/tztg/tz.htm`
——第 14 轮已注册为第 20 个官方源，本轮实测采到 **10/10**，例：
`https://eee.tju.edu.cn/info/1024/3923.htm`「市教委关于举办 2026 年天津市大学生新工科实践创新大赛的通知」。

## 2. 改了什么（都在 in-scope 文件里）

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `server/campus/sources.py` `feed_items` | 收集阶段放宽到 `max(limit, DISCOVERY_LIMIT)`，**排序后再** `items[:limit]` | 原实现先截断后排序，默认 limit 下「可读优先」形同虚设（就是上表里「同站 0 条」的来源） |
| 2 | `server/campus/sources.py` 采集循环 | 每轮候选取 `batch(state, MAX_DOCUMENTS * 4)`，**先排可读优先**，再取前 `MAX_DOCUMENTS` 条 | `backlog.batch()` 是**先截断**再返回的，只对返回的 8 条排序等于没排；实测通知公告/学业指导的原生文章在公众号队列排空前根本轮不到 |
| 3 | `server/campus/sources.py` 附件结果 | 只有 `attachment_fetch_*`（传输类、可恢复）才让整条判失败；`attachment_access_restricted`（下载验证码，结构性）只留 warning，整条判 **completed** | 文章已经抓到并存进快照，却因为附件是验证码把整条报成失败 → 五个栏目常年「全部 failed、completed 0」，每天还会把整篇重抓一遍。转瞬即逝的附件故障仍然会重试（有测试钉住） |

红测先行（改动前都能挂）：

- `test_listing_priority.py::test_the_default_budget_still_contains_the_readable_articles`
  （改前：默认预算下返回的 8 条全是公众号）
- 同文件 `CollectOrderTests::test_the_native_articles_are_attempted_within_the_per_run_budget`
  （改前：10 条公众号 + 2 条原生都已到期时，整轮 8 个名额全被公众号占掉）
- `test_attachment_interstitial_outcome.py`（3 例；改前整条 `failed`）
- 既有用例 `test_authorized_replacements.py::test_http_200_restriction_degrades_source_and_completes_the_notice`
  （原名 `…_keeps_retry_warning`）按新口径更新：来源仍 `degraded`、附件 warning 仍在快照上，
  但整条不再是 `failed`——这是本轮**有意**的行为变更，理由写在用例 docstring 里。

## 3. 对照数字（同一份 `data-operator` 副本上实跑三次 `daily_update --apply`）

| 来源（栏目） | 跑之前 | 跑之后（最终代码） | 原生文章 completed |
|---|---|---|---|
| 教务处 · 通知公告 | 20 条全 failed，completed 0 | failed 18 / **completed 2** | 0 → **2** |
| 教务处 · 学业指导 | 20 条全 failed，completed 0 | failed 17 / **completed 3** | 0 → **3** |
| 教务处 · 通知公告总览 | 15 条全 failed，completed 0 | failed 14 / **completed 1** | 0 → **1** |
| 教务处 · 学生竞赛 | 6 条全 failed，completed 0 | 6 条全 failed（该栏目确实没有原生文章） | 0 → 0 |
| 教务处 · 竞赛科创通知 | 20 条全 failed，completed 0 | 20 条全 failed（同上） | 0 → 0 |
| 新工科教育创新中心 · 竞赛通知（替身） | completed 6 / failed 4 | **completed 10 / failed 0** | （该源全是原生文章）6 → **10** |

采到的真实条目（原文 URL + 库里标题）：

```
✔ https://oaa.tju.edu.cn/info/1059/9030.htm  «2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会通知——第二批 2026-03-27»
✔ https://oaa.tju.edu.cn/info/1059/9000.htm  «《国际化拔尖创新人才培养计划》课程选课通知 2026-03-19»
✔ https://oaa.tju.edu.cn/info/1056/8760.htm  «天津大学关于启动2024级辅修专业报名工作的通知 2025-11-28»
✔ https://eee.tju.edu.cn/info/1024/3923.htm  «市教委关于举办 2026 年天津市大学生新工科实践创新大赛的通知 2026-08-28»
```

全部是官方 `*.tju.edu.cn` 上的真实文章（`column_state.py` 同时打印快照域名分布，
如 oaa 原生快照 6 条全部来自 `oaa.tju.edu.cn`）。

## 4. 红线核对（本轮没有做、也不会做的事）

- **没有绕过公众号反爬**：跑完之后公众号链接的状态仍是 `article_redirected_off_pattern`
  （通知公告 18、学业指导 17、总览 14、学生竞赛 6、竞赛科创通知 20），没有被抓成内容。
- **没有放松 SafeFetcher 地址判定**：`SafeFetcher`、`resolve_public`、逐跳复核、
  `_reject_redirected_off_pattern`、`_html_interstitial`、pattern 一行未改。
- **没有伪造 UA / cookie**：全部走产品自己的默认抓取路径；本机 TUN 场景仍靠 `config/`
  声明的官方 origin 走显式受信网关。
- **没有为凑数登记来源**：中途试过给三个栏目各加一个「只收原生地址」的来源，发现
  `test_source_catalog.py` 有「一个 origin 只能对应一个来源」的既有不变量（21 来源 / 21 origin），
  且分页页上的原生文章都是 2022–2023 的旧通知，遂**撤回**：`config/campus-sources.json`
  回到原样（15005 字节、0 个 CRLF、21 个来源），`BUILTIN_ORIGINS` 回到 20 条内置来源。

## 5. 未验证 / 限制

1. 三个栏目的原生文章是**当前列表页上的 2/3/1 条**；栏目以后是否继续发原生文章需靠日常运行观察
   （本轮没有跨天观察）。
2. `attachment_access_restricted` 依赖「URL 以 `/download.jsp` 结尾 + 页面里有 `codevalue`
   与 `createimage.jsp`」两个结构特征；站点改版后需复核。
3. 副本上跑的是 `data-operator` 的**快照副本**，真实数据目录全程只读
   （`campus.sqlite3` 的 mtime 停在 07:31 那次无人值守运行）。
4. 学生竞赛/竞赛科创通知这两个栏目，本轮证明「栏目页与其分页页都没有同站原生文章」，
   没有穷举全网（例如各学院站点是否有转载）；替身只核到新工科这一条已知通路。
5. 来源 health 仍会因附件受批判为 `degraded`（跑完之后「失败来源」里依然能看到
   `attachment_access_restricted`）——这是有意的：附件确实没拿到，只是不再拖累整条与整源。

## 6. 交给队长的两件事（不在本任务文件范围）

1. `ingestion.py::SourceBacklog.batch()` 先按 limit 截断、返回后再无排序机会，是「每轮只能看
   前 8 条」的根因；更彻底的做法是把「可读优先」下沉进 `batch()`（本任务用「多取 4 倍候选再截断」
   绕过去了，行为一样但语义分散）。
2. 建议给五个栏目补运营标注：学生竞赛、竞赛科创通知标为「**需中继通道**（栏目只发公众号）」；
   通知公告、学业指导、通知公告总览标为「原生文章已自动采集，公众号部分需中继」。
   依据是本文件第 1 节的现场抓取清单与第 3 节的对照数字。

## 7. 复现

```bash
# 现场抓取（只读；结果写入 verification/sources/columns-scan.json）
python verification/sources/scan_columns.py
python verification/sources/discover_native.py

# 栏目状态（只读打开给定库）
python verification/sources/column_state.py <campus.sqlite3> [标签]

# 副本上实测（不要指向 data-operator）
python scripts/daily_update.py --apply --data-dir <副本> --gateway-from-config
```