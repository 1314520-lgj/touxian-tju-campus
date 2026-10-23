# t19：`article_redirected_off_pattern` 逐条判定（2026-10-21 07:30 那次无人值守运行）

## 结论（一句话）

**33 条全部是真拒绝**，没有一条是误杀：它们都是教务处栏目里链接的公众号文章，
现场重放全部被微信重定向到反爬验证页 `mp.weixin.qq.com/mp/wappoc_appmsgcaptcha`，
落地页文案是「环境异常 / 完成验证后即可继续访问 / 去验证」。
**所以拒绝逻辑一个字没改**；改的是**重试节奏**——同一条被上游拒绝的链接过去每个运行都会被重新请求一次
（退避上限 1 小时），现在推迟一周（`BLOCKED_RETRY_DELAY`），因为这两种错误码本来就已被
`BLOCKED_BY_UPSTREAM` 定义为「需要授权通道，不是故障」。

## 1. 这 21/33 条到底是什么

`data-operator` 里带这个错误码的 backlog 条目共 **75 条**，按域名分组 **75/75 都是 `mp.weixin.qq.com`**
（没有任何一条是 tju.edu.cn 的同站文章 → 不存在「同站真实文章被拒」这一类）。
今天 07:30 那次运行实际尝试的条目按「最后尝试时间落在该运行窗口内」取，共 **33 条**：

| 口径 | 数量 | 说明 |
|---|---|---|
| 07:30 运行的实际尝试（本目录 `replay-rejections.json`） | **33** | 按 `last_attempt_at` 落在 23:30–23:32Z（= 07:30–07:32 本机）统计 |
| 同一运行的 `daily-20261021.json` 里各来源 `errors` 数组里的该码 | 22 | 每个来源的数组是**本轮批次**的错误码列表，受每次每源处理篇数上限影响 |
| 队长转述的「21 次」 | 21 | 与上面同源，口径是控制台按来源汇总后的数字 |
| backlog 里带该码的历史条目 | 75 | 含前几天累积的、以及同一篇文章被多个栏目同时列出的条目 |

三个数字都对，只是口径不同；本目录的逐条判定以**实际尝试的 33 条**为准（每条都能在表里看到最终地址与落地页证据）。

## 2. 逐条判定

完整表见 `verdict-table.md`（33 行，含每条的最终地址与验证页文案）；原始数据
`replay-rejections.json`（含每条 220 字落地页文本、字节数、拦截页判定）。

重放方法：用**产品自己的 `SafeFetcher`**（DNS 固定、逐跳 SSRF 复核、体积与超时限制、受信网关
都照旧，等于 `--gateway-from-config`），对每条 URL 真发一次请求，记录重定向后的最终地址与页面证据。

判定分布：

```
真拒绝：重定向到验证/反爬页   33 / 33
误杀：落地页是同一站点的真实文章   0
```

**为什么这是真拒绝**（不是只看状态码）：

1. 最终地址变成了 `https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=…&target_url=<原文章>`；
2. 落地页正文含「环境异常」「完成验证后即可继续访问」「去验证」，正文长度 < 600 字 —
   这正是第 14 轮定义的反爬验证页特征（`server/campus/content.py::_html_interstitial`）；
3. 页面里没有任何通知标题/正文段落可提取；把它存下来只会产生「环境异常」这种污染条目。

## 3. 正对照：同一条复核不会误伤同站文章

`control_native.py` 从真实快照里取了 6 条 tju.edu.cn 的原站 URL 现场重放，按采集器相同的顺序
跑一遍重定向复核：

```
job.tju.edu.cn   guard=passed  same_url=True  chars=6849  “万企进校园”天津大学2024年秋季大型双选会
news.tju.edu.cn  guard=passed  same_url=True  chars=2206  “从今天起，我想做你的一位‘新邻居’”…
gs.tju.edu.cn    guard=passed  same_url=True  chars=784   校级盲审送审材料说明202509更新-天津大学研究生院官网
ico.tju.edu.cn   guard=passed  same_url=True  chars=2027  2026年伦敦政治经济学院暑期学分项目报名通知-国际处
```

（另有 2 条样本是 `download.jsp` 附件地址，落地页 15 字「附件下载」，属附件受限那类，与本错误码无关。）

再加一层代码级论证：五个教务处来源的 `article_url_pattern` 本身就是
`^https?://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm(?:[?#]|$)|^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)` —
**公众号文章地址本身是被接受的**；只有当它被重定向成验证页、最终地址落在 `/s` 之外时才拒绝。
也就是说，如果微信哪天真的把文章发出来了，这条复核不会拦它。

## 4. 改了什么（只改重试节奏，不改判定）

`server/campus/sources.py`：

```python
BLOCKED_RETRY_DELAY = timedelta(days=7)          # 新增

except (FetchError, ValueError) as error:
    code = getattr(error, "code", "invalid_feed_item")
    failures.append(code)
    backlog.outcome(item, code)
    if code in BLOCKED_BY_UPSTREAM:
        item["next_retry_at"] = (datetime.now(UTC) + BLOCKED_RETRY_DELAY).isoformat()
```

- `BLOCKED_BY_UPSTREAM`（= `article_redirected_off_pattern`、`html_access_interstitial`）本来就已被
  `campus_collect.source_status()` 归为「blocked：需授权通道」；但 `batch()` 会重新选中所有
  `next_retry_at` 已过期的失败项，而瞬时退避上限只有 1 小时 → 一条**永远不可能成功**的链接每天都被请求。
  2026-10-21 07:30 那次就花了 33 次请求在同一个验证页上。
- 改动只影响**下一次重试的时间**：页面照样被拒（不写库、不进候选）、错误码照样记录、
  来源的 blocked 分类照样成立。
- 没动 `SafeFetcher`、没动地址安全判定、没动 `article_url_pattern`、没绕过任何验证码。

红测先行：`server/campus/tests/test_blocked_retry_cadence.py`（4 例）。关掉上面那三行后
`test_blocked_url_backs_off_for_a_week_transient_keeps_the_short_backoff` 与
`test_the_next_day_run_does_not_request_the_blocked_url_again` **失败**（退避 59 秒、次日又请求一次）；
打开后 4 例全过，相邻的反爬/ingestion/采集用例 42 例仍全过。

## 5. 对照数字

| 指标 | 修复前 | 修复后（实测） |
|---|---|---|
| 单次运行里该错误码的请求数 | 33（07:30 那次，全部落在验证页） | 同一个数据目录副本上，连着跑两次：**第一次 37 条被拒项被尝试（其中 11 条给出该码），第二次该码 13 条，全部是本轮新发现或“先瞬时失败、后拿到验证页”的条目** |
| 已被推迟的条目在同一副本的下一次运行里被重复请求 | —（旧策略退避 1 小时，下一次就会重来） | **0**（对照脚本 `ab_compare.py`：`run A 尝试过 37 条 → run B 只有 26 条，其中 9 条是 A 里瞬时失败的，17 条是新发现；被推迟的 63 条一条没重来） |
| 下次重试时间 | ≤ 1 小时 | **7 天**（副本里 63/75 条已推迟到 `2026-10-28T01:04Z`；余下 12 条是本次批次没轮到的，会在轮到的那一轮被推迟） |
| 因此新增入库的真实文章 | — | **0 条**：33 条全是验证页，没有一条是“本该入库却被拒”的真实文章 |

> 说明：因为不存在误杀，「新增入库」必然是 0。这一节的意义是——**没有任何内容因为不改判定而被放弃**，
> 而每天的无效请求从「每轮都重来」变成「每周一次」。

## 6. 真拒绝为什么保持（不得为了多采而放宽）

- 反爬验证页不是通知：它的正文是「环境异常」，把它入库就是第 14 轮修掉的污染；
- 绕过它需要伪造身份/Cookie，产品纪律明确禁止（`SOUL.md`、AGENTS.md 的「边界」节）；
- 公众号文章的**合规通道已存在**：运营方中继（`deploy/operator/relay.py`）转录后由产品自己的
  SafeFetcher 原样采集，`data-operator` 里「运营中继 · 教务处竞赛科创通知」的 2 条已是 `completed` ✓
  —— 也就是说这些内容不是「永远拿不到」，而是「需要授权通道」。

## 7. 需要交给队长的两件事（不在本任务 in-scope）

1. **五个教务处栏目现在只列公众号文章**：`dropped_native.py` 实时抓栏目页发现每页 8 条链接、
   同站文章 0 条（`competition/notice` 五个来源皆然）。它们的 backlog 因此长期是
   「全部 failed、completed 0」，每天都会产生该错误码。建议要么把这几列明确标注为
   「公众号栏目 → 需中继」，要么为它们补一个原生同源栏目（第 14 轮的 eee.tju.edu.cn 就是先例）。
2. `ingestion.py`（不在本任务文件范围内）的更彻底做法是让 `batch()` 直接跳过
   `error_code in BLOCKED_BY_UPSTREAM` 且可判定为终态的条目；本轮用 `next_retry_at` 推迟
   达到了同样效果，但「重试上限」的语义仍分散在两处（`outcome()` 的 attempts 上限与 `batch()` 的选择）。

## 8. 未验证 / 限制

1. **没有跨周观察**：7 天推迟是按代码与副本状态推出来的，没有真的等到下周看它是否只请求一次。
2. 副本上的两次运行相隔约 10 分钟；「下一次运行」在报告里既指 10 分钟后的实跑，也指次日计划任务，
   两者的差别只在 `next_retry_at` 是否已到期，已用 ingestion 时钟做次日模拟（红测里）。
3. 重放用的是本机出口；微信改版后验证页特征可能变化（那时 `_html_interstitial` 与最终地址复核需要复核）。
4. `ab_compare.py` 的窗口判定按 `last_attempt_at` 字符串前缀比较，跨秒边界可能把个别条目算到相邻窗口。

## 9. 本目录文件

| 文件 | 内容 |
|---|---|
| `verdict-table.md` | 33 条逐条判定表（最终地址 + 落地页证据） |
| `replay-rejections.json` | 重放原始数据（每条含落地页文本前 220 字、字节数、拦截页判定） |
| `replay-run.log` | 重放过程的控制台输出 |
| `control-native.json` | 正对照（同站文章不被误拒） |
| `rejected-urls.json` | backlog 里 75 条被拒条目的 URL/标题/尝试次数 |
| `dropped-native.json` | 五个教务处栏目的实时栏目页 vs 队列对比 |
| `replay_rejections.py` / `control_native.py` / `list_backlog.py` / `dropped_native.py` / `duplicate_wechat.py` / `ab_compare.py` / `make_verdict_table.py` | 复现脚本（都用只读方式打开 `data-operator`，实验数据写在副本里） |
| `recent-article_redirected_off_pattern-2026-10-20T23.json` | 那次运行的 fetch_run 记录（只记到每个来源的错误码计数，逐条 URL 在 backlog 里） |
| `dump_recent.py` / `list_rejected.py` / `probe_collections.py` / `show_backlog.py` / `extract_errors.py` / `backlog_breakdown.py` | 排查过程脚本：先找错误码记在哪、再确认逐条 URL 在 `source_ingestion.items`（留作下一轮复用） |
| `replay-run.log` | 33 条重放的控制台原始输出 |