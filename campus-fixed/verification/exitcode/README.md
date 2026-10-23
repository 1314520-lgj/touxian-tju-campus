# t22：日常更新的退出码语义（跳过 / 失败 / 降级）

## 一句话

2026-10-21 07:30 的无人值守日常更新**退出码 1**，而它唯一的两个 `failed` 来源是两个
**没开的中继 feed**（`feed_https_required`）。这类「通道没配好」现在计入**跳过**，
不再计入失败；真实失败仍然退 1，并由新增的红测守住这条边界。

## 一、根因（实读 07:30 的真实日志，不是回忆）

`data-operator/logs/daily-20261021.json` 的 collect 段（只读打开）：

| 口径 | 数字 |
| --- | --- |
| 来源状态 | ok 12 · degraded 6 · blocked 2 · **failed 2** |
| 两个 failed 是谁 | `运营中继 · 教务处竞赛科创通知`、`运营中继 · 本科招生网通知公告` |
| 它们的错误码 | `feed_https_required`（各 1 条） |
| parse_errors / candidate_errors | 全部为空 |

旧的退出码就是 `scripts/campus_collect.py` 的 `exit_code(totals)`：
`totals["failed"]` 非 0 就退 1。于是「中继今天没开」每天都会让命令退 1，
而真正的 `article_redirected_off_pattern`（21 条，t19 已判定 0 误杀）与
`attachment_access_restricted`（38 条，本来就不绕过）都不计失败——
警铃每天响，真出事时反而看不出区别。

`feed_https_required` 是 `Sources.config()` 在**任何抓取之前**抛出的：该来源是授权 feed
（`feed_content_mode=authorized_fulltext`）而 origin 是 http 且不在受信网关。它描述的是
**配置事实**（中继没跑 / origin 没被显式放行），不是站点故障。

## 二、改法（只动 `scripts/daily_update.py`）

1. 新增证据常量与判定函数（不看来源名字，只看错误码）：

```python
OPTIONAL_CHANNEL_CODES = frozenset({
    "feed_https_required",
    "feed_mode_not_enabled",
    "feed_mode_requires_feed_adapter",
    "feed_authorization_not_verified",
})

def optional_channel_only(row):     # 该行的所有证据都是「通道未启用」
def row_failure_codes(row):         # 源取不到 / 正文解析失败 / 抽取失败
def classify_rows(rows):            # → skipped / failures / degraded，各自带错误码与原因
```

2. `collect_stage` 用 `classify_rows` 的结果决定退出码：
   `EXIT_FAILED if verdict["failures"] else EXIT_OK`（不再直接用 `campus_collect.exit_code`）。
3. 报告里三类分开打印，且**都带错误码与原因**：

```text
  来源        22 个：成功 11 · 降级 7 · 需授权 2 · 跳过 2 · 失败 0
    跳过来源  运营中继 · 教务处竞赛科创通知：可选来源未启用（feed_https_required）：通道没开，不是故障
    降级来源  天津大学国际处 · 学生项目通知：设计内的降级（attachment_access_restricted）：不绕过，通知本身已采到
  判据        跳过 = 只带「可选通道未启用」错误码；失败 = 源取不到/正文解析失败/抽取失败；降级 = 设计内拒绝（如附件要授权），不影响退出码
```

4. 退出码那一行带解释：`退出码 0（干净：2 个可选来源未启用，只算跳过）`。

**没有**改 `server/campus/`（含 `campus_collect.py` 的 `source_status`/`exit_code`），
也没有动 `RELEASE_MANIFEST.json`。

## 三、红测（新增文件，未改任何既有测试）

新文件：`server/campus/tests/test_daily_exit_codes.py`（11 个用例 + 9 个子用例，独立于
t21 正在改的目录内容；`test_daily_update.py` 只读复用，未改一个字节）。

| 用例 | 断言 |
| --- | --- |
| 中继未开（`feed_https_required`） | 退出码 **0**；跳过列表含该码；输出有「跳过来源」「通道没开，不是故障」「失败 0」 |
| 中继从未发出请求 | fetcher 调用列表为空——证明这是抓取前的配置事实，不是被吞掉的故障 |
| 授权依据未记录（`feed_authorization_not_verified`） | 退出码 **0**（同一类证据） |
| 源取不到（`dns_failed` / `fetch_transport_failed` / `upstream_http_500` / `upstream_http_404` / `unsafe_address` / `unsafe_url`） | 退出码 **1**；失败列表含该码；跳过列表为空；输出有「失败来源」 |
| **正文**解析失败 | 退出码 **1** |
| 抽取失败 | 退出码 **1** |
| 名字就叫「运营中继 · 教务处竞赛科创通知」但真失败 | 退出码 **1**，且不在跳过名单里（**反名字启发式**） |
| feed **已启用**但内容坏（`invalid_feed_content` / `feed_fulltext_missing` / `feed_receipt_missing_or_changed`） | 退出码 **1**（跳过规则很窄，不是「凡是 feed 都跳过」） |
| 跳过 + 真失败同一次运行 | 退出码 **1**（跳过不掩盖失败），两条都在输出里 |
| 干净运行（列表+正文都正常） | 退出码 **0**，且**没有**「跳过来源」字样 |
| 设计内拒绝（`article_redirected_off_pattern`） | 退出码 **0**，但错误码仍然打印出来 |

红测先行：改动前的口径用**未被改动的** `campus_collect.exit_code` 在同一份夹具上复现
（`.review-t12-verify/red-proof-t22.py`，仓库外）：

```text
[A 中继未开]   旧口径：status=['failed'] totals.failed=1 → 退出码 1   ← 这就是「天天退 1」的根因
[B 普通来源不可达] 旧口径：status=['failed'] totals.failed=1 → 退出码 1   ← 这条必须保持 1
```

## 四、真实数据上跑一次（data-operator 的副本；原目录只读）

`python scripts/daily_update.py --data-dir <data-operator 副本> --gateway-from-config --apply --json-out …`
（88 秒，真实网络抓取 22 个来源）：

```text
数据目录：ec-copy
受信网关：config/campus-sources.json（13 个 origin，仅本进程）
[1/3] 采集
  来源注册    新增 0 · 复用 22
  来源        22 个：成功 11 · 降级 7 · 需授权 2 · 跳过 2 · 失败 0
  入库        新增快照 27 · 新候选 14 · 解析 ok 0/partial 14/failed 0
    跳过来源  运营中继 · 教务处竞赛科创通知：可选来源未启用（feed_https_required）：通道没开，不是故障
    跳过来源  运营中继 · 本科招生网通知公告：可选来源未启用（feed_https_required）：通道没开，不是故障
    降级来源  天津大学国际处 · 学生项目通知：设计内的降级（attachment_access_restricted）：…
    降级来源  天津大学教务处 · 学业指导：设计内的降级（article_redirected_off_pattern、attachment_access_restricted）：…
    降级来源  天津大学智能与计算学部 · 通知公告：设计内的降级（unsafe_address）：…
    降级来源  天津大学研究生院 · 学位通知：设计内的降级（upstream_http_404）：…
  错误码      article_redirected_off_pattern×20, attachment_access_restricted×37, feed_https_required×2, unsafe_address, upstream_http_404
[2/3] 复核
  待复核      142 条 → 自动接受 1 · 拒绝 11 · 留给人 130
  发布        新增发布 3 条 · 核对已接受候选 24 条
[3/3] 下架
  过期事项    0 条（已下架）· 非通知 0 条（已下架）

退出码 0（干净：2 个可选来源未启用，只算跳过）
```

- **失败列表为空 → 退出码 0** ✓（同一批数据在旧口径下会退 1：07:30 的 `failed 2` 就是这两个 feed）。
- 跳过与降级都逐个打印了错误码，没有隐藏。
- `data-operator` 全程只读（用副本跑 `--apply`）；副本跑完已删除。

契约给的演练命令（全新目录、只演练）也跑了：
`python scripts/daily_update.py --data-dir .test-temp-exitcode --gateway-from-config` → 退出码 0，
打印「数据目录尚不存在：这是首次运行 / 将采集 20 个启用来源」（该路径不抓取，因此没有跳过/失败列表）。

## 五、文档

`docs/REAL-DATA-RUNBOOK.md` §5：

- 新增「退出码怎么读：跳过 / 失败 / 降级」表（判据 = 错误码与连接结果，退出码 0/1/2 的含义）。
- 错误码表补上 feed 家族：`feed_https_required` 等四个 → **跳过**；
  `feed_fulltext_missing` / `invalid_feed_content` / `feed_receipt_*` / `feed_original_*` → **失败**。
- 修掉一处与实现不符的旧描述（「首轮 oaa 全失败」段落原文说「退出码 1」，实际
  `article_redirected_off_pattern` 属 blocked，从来不计失败）。
- 明写一条边界：**所有**来源都被设计内拒绝挡住（没有任何源成功、也没有新快照）时，
  当前实现仍然退 0——与改动前一致，本轮没有改。

## 六、已验证 / 未验证

**已验证**

- 新文件 11 个用例 + 9 个子用例全过（`pytest server/campus/tests/test_daily_exit_codes.py -q`）。
- 与 t9 的既有用例一起过：`pytest server/campus/tests/test_daily_exit_codes.py server/campus/tests/test_daily_update.py -q`
  → **27 passed**（未改 `test_daily_update.py` 一个字节）。
- 全量回归：`python -m pytest server/campus/tests -q --basetemp=.test-temp-exitcode-gate/basetemp2`
  → **902 passed / 8 skipped / 0 failed / 143 subtests**，退出码 0（2026-10-21 11:17，PYTHONUTF8=1）。
- 真实数据实跑一次：退出码 0、跳过 2、失败 0、降级 7、需授权 2、成功 11。
- 红测先行有据：旧口径在同一夹具上对「中继未开」也退 1（仓库外脚本复现）。

**未验证 / 边界**

- 中继**真的开着**时这四条 feed（含 `feed_fulltext_missing` 等）没有被跑过一遍；
  它们按错误码走 `row_failure_codes` 一律算失败，但没有真实中继的端到端复现。
- `OPTIONAL_CHANNEL_CODES` 里的 `feed_mode_not_enabled` /
  `feed_mode_requires_feed_adapter` 在每日采集这条路径上**不可达**（`require_feed_authorization`
  只在 fulltext 来源上调用，而它内部判定又会先放行），保留是防御性的；测试只覆盖了
  另外两个（`feed_https_required`、`feed_authorization_not_verified`）。
- **全源都被 blocked/degraded**（无任何 `ok` 来源、无新快照）时仍退 0 —— 与改动前一致，
  本轮未改；是否需要单独报警留给队长决定。
- 附件级失败（如那条 2015 年明文 http 死链的 `unsafe_address`、老 `.doc` 解析失败）
  一律算降级、不影响退出码。源级 `unsafe_address`（列表页本身被安全判定拒绝）仍然退 1
  （红测覆盖）。这是本轮明确的选择：否则每天都会为一条死链响铃。
- 没有在真实计划任务（07:30）上重跑：改的是命令逻辑，下一次计划任务自然生效。