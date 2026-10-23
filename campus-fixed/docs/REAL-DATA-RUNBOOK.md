# 真实数据运行手册：初始化 · 日常采集 · 受信网关

> 适用对象：在本机运营「偷闲·天大版」的运营者 / 下一个接手的 AI。
> 目标：**全新数据目录一条命令起一套真实数据**，之后每天一条命令增量采集，
> 产出的候选进入人工复核队列；不碰演示数据，不放宽抓取层安全判定。
>
> 相关文档：仓库入口 `AGENTS.md`、运营中继契约 `deploy/operator/OPERATIONS.md`、
> 第 14 轮缺陷诊断 `docs/DIAGNOSIS-REAL-USE.md`。

---

## 0. 一句话版本

```powershell
# 仓库根目录；$PY 是装齐依赖的解释器
& $PY scripts/campus_collect.py init --data-dir data-operator --gateway-from-config
```

之后每天：

```powershell
& $PY scripts/campus_collect.py collect --data-dir data-operator --gateway-from-config
```

`--gateway-from-config` 只把 `config/campus-sources.json` 里已声明的官方 origin
填进 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`（仅本次进程），不改变 `SafeFetcher` 的
任何地址安全判定。原因见第 2 节。

---

## 1. 脚本做了什么

`scripts/campus_collect.py` 直接调用产品自己的 `Core` / `Pipeline`，不经过 HTTP、
不需要登录、不触发 dev-auth 限流：

| 步骤 | 产品内实现 | 说明 |
| --- | --- | --- |
| 注册/复用来源 | `Pipeline.sources.seed()` | 幂等；已存在的来源不覆盖 |
| 逐源采集 | `fetch_source({source_id})` | 真实网络请求，走 SafeFetcher |
| 解析 | `parse_document({snapshot_id})` | 正文片段、警告、解析状态 |
| 抽候选 | `extract_candidates({snapshot_id})` | 截止时间/材料/入口/事件类型/证据链 |
| 统计 | 脚本汇总 | 成功/降级/失败源、新增快照、候选、待复核、错误码、积压 |

两个子命令：

- `init`：创建数据目录 → 注册 19 个官方源 → 跑第一次采集。
  `--no-collect` 只注册不联网。
- `collect`：日常采集；每次都会先做一次幂等的来源注册/复用。

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--data-dir` | 数据目录；缺省读 `CAMPUS_DATA_DIR`，再缺省 `data-operator/` |
| `--source` | 只采集指定来源（id / 全名 / 名称片段），可重复；选不中或含义不唯一会报用法错误。示例：`--source 智能与计算`、`--source 培养办事流程`、`--source 3dd0449a-a81d-5199-a345-9d04a5da1f6e`。**不是** `source_identity` 短键：`--source cic` / `--source oaa` 匹配不到，会报「未找到来源」 |
| `--json-out` | 机器可读摘要（验收/巡检留档） |
| `--gateway-from-config` | 用配置里的官方 origin 填受信网关（仅本进程） |
| `--skip-dns-check` | 跳过 fake-IP 预检（离线/测试用） |
| `--timeout` | 单次 HTTP 请求超时秒数，默认 20 |

**幂等性**：快照 ID 由 `source_id + canonical_url + content_hash` 决定
（`sources.persist`）。同一篇通知内容未变时重复采集不会再入库、不会产生新候选；
只有内容变化才会产生新快照与新候选版本。脚本不删除任何数据。

**采集节奏**：每次每个源最多处理 8 篇（产品自身的 `MAX_DOCUMENTS`）。栏目页
其余条目进入来源积压队列，摘要里以 `积压 N 待抓` 显示，后续每天自然补齐；
不会一次性把 100 条列表全部拉完。

**退出码**：`0` 没有源级失败（允许降级：部分文章/附件失败）；`1` 至少一个源失败；
`2` 用法或数据目录被拒绝。失败源一定打印错误码，不会静默。

---

## 2. 本机 fake-IP 与 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`

### 现象

本机经代理 TUN 访问网络时，官方域名解析到保留地址：

```text
oaa.tju.edu.cn  -> 198.18.0.200
gs.tju.edu.cn   -> 198.18.0.207
news.tju.edu.cn -> 198.18.0.203
```

产品的 `SafeFetcher`（`server/campus/sources.py`）在 `resolve_public()` 里拒绝
非全局地址，于是真实采集会得到 `unsafe_address`。**这是防 SSRF 的正确行为，
不要改它**：放宽它等于允许抓取内网/元数据地址。

### 唯一合规做法

`CAMPUS_TRUSTED_GATEWAY_ORIGINS` 是既有的、按**精确 origin** 生效的白名单。
它只放宽「解析结果必须是全局地址」这一条，并且：

- 云元数据地址（`100.100.100.200`、`fd00:ec2::254` 及其 IPv4 映射）永远拒绝；
- link-local / multicast / unspecified 永远拒绝；
- 未列出的 origin 完全不受影响；
- 不做 DNS rebinding 放行、不跳过 TLS 主机名校验、不伪造 UA/cookie。

### 两种设置方式

**A. 交给脚本（推荐，最小命令）**

```powershell
& $PY scripts/campus_collect.py collect --data-dir data-operator --gateway-from-config
```

origin 列表来自 `config/campus-sources.json`（产品已声明为官方的来源），
外加 `https://mp.weixin.qq.com`。加公众号主机是为了让抓取层能**如实拒绝**
微信反爬页（`html_access_interstitial` / `article_redirected_off_pattern`），
而不是把它误报成 `unsafe_address`；信任主机不等于绕过反爬，抓取层仍然拒绝。

**B. 手工设置环境变量**

```powershell
$env:CAMPUS_TRUSTED_GATEWAY_ORIGINS = @(
  "https://oaa.tju.edu.cn",
  "https://gs.tju.edu.cn",
  "https://news.tju.edu.cn",
  "https://cic.tju.edu.cn",
  "https://job.tju.edu.cn",
  "https://ico.tju.edu.cn",
  "https://me.tju.edu.cn",
  "https://scet.tju.edu.cn",
  "https://zs.tju.edu.cn",
  "https://yx.tju.edu.cn",
  "https://mp.weixin.qq.com"
) -join ","
```

已在环境里的值优先，脚本不会再覆盖。

### 不要做

- 修改 `resolve_public` / `SafeFetcher` 的地址判定；
- 把 `0.0.0.0/0`、`198.18.0.0/15` 之类的网段当成「受信网关」；
- 伪造 UA、Cookie、`poc_token` 去换微信验证页；
- 用 `--no-verify` 之类不存在的开关自我安慰。

---

## 3. 启动 operator 实例（人工复核与发布）

采集只产出 `pending` 候选；**没有任何东西会自动发布**。复核与发布需要
operator 实例（本机回环、开发登录码只在本机可见、绝不播种合成数据）：

```powershell
$env:CAMPUS_CLOUD_MODE = "operator"
$env:CAMPUS_DATA_DIR = (Join-Path $PWD "data-operator")
$env:CAMPUS_TRUSTED_GATEWAY_ORIGINS = "https://oaa.tju.edu.cn,https://gs.tju.edu.cn,https://news.tju.edu.cn,https://cic.tju.edu.cn,https://job.tju.edu.cn,https://ico.tju.edu.cn,https://me.tju.edu.cn,https://scet.tju.edu.cn,https://zs.tju.edu.cn,https://yx.tju.edu.cn,https://mp.weixin.qq.com"
$env:PORT = "8000"
& $PY serve_cloud.py
```

打开 `http://127.0.0.1:8000`，开发登录码显示在登录页（仅回环实例开启）。
复核队列是 `/admin/reviews`：返回的是 **candidate**，主键 `candidate_id`；
决策必须带 `expected_candidate_revision`。人工接受后才会写入发布 outbox。

---

## 4. 日常采集与定时

### 4.0 一条命令：采集 → 复核 → 下架

三段（采集、复核发布、下架）现在是一条命令，无人值守也能跑：

```powershell
# 演练（默认）：只读，不改任何数据
& $PY scripts/daily_update.py --data-dir data-operator --gateway-from-config

# 真正执行（写库）：计划任务跑的就是这一条
& $PY scripts/daily_update.py --apply --data-dir data-operator --gateway-from-config ^
  --json-out data-operator/logs/daily-manual.json
```

- 三段各有独立 `try/except`：某个来源抓不到，不会让复核和下架跟着停摆。
- 同一个数据目录只允许一个实例：文件锁 `data-operator/.daily-update.lock`，后到者立刻退出并说明原因，
  不排队、不并行写库（进程崩了锁由操作系统释放，不会永久锁死）。
- 退出码：`0` 全部正常；`1` 有失败但未阻断（部分来源失败、复核有拒绝）；`2` 用法错误。
  **`1` 不等于「白跑一趟」**：跑通的段落已经落库，日志里逐段写明。
- 只看一条来源排查时，用 `--source <id|名称片段>`（见 5. 错误码一节）。

### 4.0.1 分开跑（排查用）

```powershell
# 全部启用的来源
& $PY scripts/campus_collect.py collect --data-dir data-operator --gateway-from-config --json-out verification/collect-latest.json

# 只跑一个源（排查用；id / 全名 / 名称片段均可）
& $PY scripts/campus_collect.py collect --source 智能与计算 --gateway-from-config
& $PY scripts/campus_collect.py collect --source 培养办事流程 --gateway-from-config

# 全新目录 + 第一次采集
& $PY scripts/campus_collect.py init --data-dir data-operator --gateway-from-config
```

### 4.0.2 人工复核工作台（把「留给人判断」的队列过完）

日常更新里的三态策略**故意保守**：只自动接受「今天仍可办」（还有未来期限）的候选，
其余留在队列里等人。2026-10-21 07:30 的真实运行是 `待复核 140 条 → 自动接受 1 · 拒绝 12 ·
留给人 127` —— 这 127 条就是这条命令要处理的对象。

```powershell
# 1) 看队列：默认按「标题 + 期限集合」分组，一组一件事（只读，不改数据）
& $PY scripts/review_queue.py --data-dir data-operator --limit 10
& $PY scripts/review_queue.py --data-dir data-operator --limit 0        # 全部 127 条 → 94 组
& $PY scripts/review_queue.py --data-dir data-operator --limit 0 --flat # 老样子：一行一条（127 行）

# 2) 看一条的完整依据：期限/材料引文、原文链接、以及正文里出现但抽取没认成期限的日期线索
& $PY scripts/review_queue.py --data-dir data-operator --show <candidate_id>

# 3) 单条决策（必须带理由；理由写进 reviews 审计）
& $PY scripts/review_queue.py --data-dir data-operator --accept <id> --reason "真实官方通知，依据完整" --apply
& $PY scripts/review_queue.py --data-dir data-operator --reject <id> --reason "新闻稿，不是学生可办事项" --apply

# 4) 整组决策：--group 给组内任一 id，先把组内候选 id 全列出来，确认后加 --yes --apply
& $PY scripts/review_queue.py --data-dir data-operator --group <id> --accept --reason "官方通知，依据完整"
#   不带 --yes 时只打印将影响的候选 id 列表并返回退出码 2（什么都没改）

# 5) 批量：只能显式给 id、筛选条件或整组；先打印将影响的条目，加 --yes 才执行
& $PY scripts/review_queue.py --data-dir data-operator --batch-ids "<id1>,<id2>" --reject --reason "…"
& $PY scripts/review_queue.py --data-dir data-operator --batch-filter tier=3 --batch-filter notice=no --reject --reason "…"
#   上面两条不带 --yes --apply 时只打印计划并返回退出码 2（什么都没改）；确认后补上这两个开关
```

**队列分组（2026-10-21 实测）**：同一件事会被多个来源、以及反复抓取各存一份候选，
所以 127 条待判断其实只有 **94 件不同的事**（按「归一化标题 + 期限集合」；
只按标题去重同样是 94 组；19 个重复组涉及 52 条，最多的一组 4 条）。
分组只是这一屏的排布：候选、快照、审计一条不动，`--flat` 随时回到一行一条，
`--show <组内任一 id>` 照常可用，决策打下去仍然是**逐条候选 id**（整组会把 id 全列出来并要求 `--yes`）。

判定口径没有新增：列表里的三态结论来自 `scripts/review_and_publish.py` 的 `classify()`，
决策走 `Pipeline.review`（就是 `/admin/reviews/{id}/decision` 背后的同一个函数），
审计写进 `reviews`（含 decision、reason、reviewer_id、时间）。

排序与安全：

- 排序按「更可能值得人看」：有未来期限 > 抽取判定需要行动 > 带材料 > 标题是通知/公告 >
  官方域名 > 近期抓到；明显是长期参考资料或新闻的沉到底部，并逐条打印**为什么沉底**（打分依据）。
- 队列里 127 条**全部**是 `review`，且**没有一条有期限证据**（2026-10-21 实测：有未来期限 0 条）——
  这正是它们留给人判断的原因，工作台把这一句写在每一条上，并说明「机器为什么既不能接受、也不能拒绝」。
- 没有「全部接受」入口：批量必须 `--batch-ids`、`--batch-filter` 或 `--group`；超过 `--max-batch`（默认 25）
  直接拒绝；不带 `--yes` 只打印计划。默认演练，只有 `--apply` 才写。分组同理：整组决策先把组内
  候选 id 全列出来，仍然逐条走 `Pipeline.review`。
- 只读命令（列表/详情、以及所有演练）不会改动数据目录：签名密钥不会新建，锁文件只在真正要写之前才取。

实测（2026-10-21，在 `data-operator` 的**副本**上执行；原目录只读查询）：

| 指标 | 处理前 | 处理后 |
|---|---|---|
| 待人工判断 | 127 | 120 |
| 已接受候选 | 23 | 25 |
| 事件版本合计（已发布版本） | 529 | 531 |
| `reviews` 审计行 | 973 | 980 |
| 公开（active）事项 | 122 | 122（两条接受命中的是既有事项，只升版本，没有凭空多出事项） |

已处理记录示例（理由原文即审计里的 reason）：

- 接受 `431f1c74…`「2026年秋季学期交换生（第二批）项目通知」：真实官方项目通知（国际处），
  正文含报名要求；抽取没拿到期限，保留为可检索事项。
- 拒绝 `e6d175a1…`「长征胜利90周年」：新闻稿（校史纪念），不是学生可办事项。
- 批量拒绝（筛选 `tier=3` + `title=天津电视台`，2 条）：电视台新闻转载，不是校内可办事项。

已验证 / 未验证：

- **已验证**：列表与排序、详情（期限/材料引文、原文链接、日期线索）、单条接受/拒绝、
  批量（显式 id 与筛选两种入口）、不带 `--yes` 只预览、超限拒绝、未知 id/筛选键拒绝、
  决策进入 `reviews` 审计、策略未被放宽；只读命令跑完对数据目录做逐文件 sha256 对照无内容变化
  （只有 sqlite 的临时 `-wal/-shm` 在连接关闭时被清理，这是 sqlite 自己的行为）。
- **未验证**：真人过完 127 条的实际耗时（没有做人工计时实验）；`--json` 输出没有接下游消费者；
  `--sort deadline/recent/source` 三种排序只做了目测；没有在 8000/8501 之外的端口起网页版复核台对比。
- **没有新增自动化测试**：本轮 inScope 是 `scripts/` 与本文档，`server/campus/tests/` 不在其中，
  所以这次的红测是手工回归矩阵（9 个安全用例 + 7 条真实决策），而不是新增的 pytest 用例；
  共用判定口径的既有用例（`test_daily_update.py` 等）仍然覆盖自动复核那一段。

### 4.1 每天清理过期事项（采集后跑一次）

这是给同学日常用的产品，**截止时间已过的事项必须下架**，否则清单会被几年前的旧通知占满。

```powershell
# 先演练：只列出将下架的事项，不改动任何数据
& $PY scripts/campus_collect.py prune --data-dir data-operator --skip-dns-check

# 确认无误后执行（写审计记录，可用 /admin/events 改回 published 恢复）
& $PY scripts/campus_collect.py prune --data-dir data-operator --apply --skip-dns-check ^
  --json-out verification/prune-latest.json
```

判定规则（保守，不会误伤）：

| 事项 | 处理 |
|---|---|
| 所有截止时间都已过去 | **下架** |
| 有任何一条截止时间在今天或以后 | 保留（仍然可办） |
| 一条具体日期都没有 | 保留（长期参考，如「学位认证操作步骤」） |

下架用的是产品自带的 `withdrawn` 状态：会写 `publication_audits`（含操作者与理由）、
发 `public_event.changed` 事件、并从清单／公开列表／检索／问答中消失；记录与证据仍在库里，
需要时可恢复。**不是物理删除**，所以订阅者不会看到指向不存在事项的链接。

### 4.2 每天自动跑（Windows 计划任务）

手写 `schtasks /Create` 容易漏掉「不依赖登录会话」「不并发」这些关键设置，所以用仓库里的安装脚本：

```powershell
# 安装 / 更新（需要管理员 PowerShell；重复执行是「更新」，不会报「任务已存在」）
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install

# 改触发时间：再 -Install 一次，带上 -Time（24 小时制）
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install -Time 08:15

# 看状态 + 最近一次执行摘要（时间、三段统计、退出码）
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status

# 立刻跑一次（手工排障；计划任务干的就是这件事）
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -RunNow

# 卸载（只删任务，日志保留）
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Uninstall
```

装好之后 `schtasks /Query /TN TouxianCampusDaily` 应看到每天 07:30、状态 `Ready`：

| 注册项 | 值 | 为什么这么设 |
|---|---|---|
| 触发 | 每天 07:30（`CalendarTrigger` + `DaysInterval 1`） | `-Time` 可改 |
| 运行身份 | 当前用户，`LogonType=S4U` | **不依赖登录会话**：合上笔记本再开机、没人登录也会跑 |
| 多重实例 | `MultipleInstancesPolicy=IgnoreNew` | 上一轮没跑完时，这一轮不会再起一份 |
| 错过补跑 | `StartWhenAvailable=False` | **关机那天不补跑**，见 4.3 |

解释器不是「随便挑一个 python」：脚本按 `-Python` → `CAMPUS_PYTHON` → 现有任务里记录的那个 →
仓库 venv → `PATH` → `%LOCALAPPDATA%\Programs\Python\Python3*` 的顺序找，**每个候选都要真的
`import fastapi, bs4, sqlalchemy` 成功才采用**，被跳过的候选连同原因一起打印（本机 PATH 里的
Python311 就缺 fastapi，会被明确列出来而不是静默采用）。注册前还会用真实命令行跑一次
`--dry-run` 演练（不写数据）：演练不过就不注册——宁可没有任务，也不要装一个每天静默失败的任务。

**临时停用 / 恢复**：

```powershell
schtasks /Change /TN TouxianCampusDaily /DISABLE   # 停用（任务还在，只是不再触发）
schtasks /Change /TN TouxianCampusDaily /ENABLE    # 恢复
schtasks /End    /TN TouxianCampusDaily            # 立刻停掉正在跑的那一次
```

**看日志**（都在数据目录里，不随发布包出厂）：

| 文件 | 内容 |
|---|---|
| `data-operator/logs/daily-YYYYMMDD.log` | 当天每次执行的完整输出：开始/结束时间、解释器、完整命令行、三段统计、退出码；同一天跑多次就追加多段 |
| `data-operator/logs/daily-YYYYMMDD.json` | 同一批次的机器可读摘要（`--json-out`），`-Status` 优先读它 |
| `data-operator/logs/last-run.json` | 最近一次执行的时间、用时、退出码、日志位置 |

`-Status` 会把这三样揉成一段可读摘要；要看原始文本就
`Get-Content data-operator\logs\daily-YYYYMMDD.log`。

### 4.3 「关机那天不会补跑」

任务只在计划时刻触发（`StartWhenAvailable=False`）。晚上关机或休眠，那一天的自动更新就跳过了，
**下次开机不会补跑**——因为补跑等于在一个已经过期的时刻去抓「今天」的通知，容易把旧内容当成新内容。

- 长期开机的机器：装完不用管。
- 经常关机的机器：开机后手动补一次
  `python scripts/daily_update.py --apply --data-dir data-operator --gateway-from-config`，
  或者 `schtasks /Run /TN TouxianCampusDaily` 让计划任务立刻跑一次。
- 确实想补跑：在「任务计划程序」GUI 里打开该任务的属性，勾上「如果错过计划开始时间，请尽快启动任务」
  （等于 `StartWhenAvailable=True`；安装脚本目前固定为 False）。

Linux/macOS 用 cron 顶替同一条命令（`0 7 * * * cd <仓库根> && <python> scripts/daily_update.py --apply --data-dir data-operator --gateway-from-config`）；
那里没有 S4U，但 crontab 本来就跟着机器走。

---

## 5. 输出与错误码怎么读

真实运行示例（完整输出见 `verification/real-collect-init-2026-10-19.log`）：

```text
校园通知真实采集 · init
数据目录：.campus-t6-real
来源注册：新增 19 · 复用 0
受信网关：config/campus-sources.json 官方来源（11 个 origin，仅本进程）

逐源结果（19 个）
  [ 1/19] ok       天津大学化工学院 · 本科教学通知
          新增快照 2 · 解析 2/0/0 · 新候选 2
...
汇总
  来源        19 个：成功 6 · 降级 8 · 失败 5
  入库        新增快照 181 个 · 文档总数 181
  解析        ok 32 · partial 71 · failed 78 · 无正文跳过 1
  候选        新增 96 · 合计 96 · 待复核 96
  错误码      article_redirected_off_pattern×5, attachment_access_restricted×75, unsafe_address×4
  积压        待抓 126 · 失败待重试 80（每次每源最多处理 8 篇，后续每天自然补齐）
  复核入口    operator 实例 /admin/reviews（需人工逐条决策后才会发布）
```

（上面的数字是第一次真实运行的快照，会随官网更新变化；以日志为准。）

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `unsafe_address` | DNS 落到非全局地址，且该 origin 不在受信网关 | 按第 2 节配置受信网关；不要改 SafeFetcher |
| `dns_failed` | DNS 无答案 | 检查代理/TUN、域名是否变更 |
| `fetch_timeout` / `fetch_transport_failed` | 网络超时/中断 | 稍后重跑；该类失败在积压里自动退避重试 |
| `upstream_http_4xx/5xx` | 官网返回错误 | 观察是否临时故障；4xx 持续说明栏目地址已变 |
| `article_redirected_off_pattern` | 详情页被 302 到反爬/验证页（常见微信 `wappoc_appmsgcaptcha`） | **正确拒绝**，该页不会进候选；不要绕过 |
| `html_access_interstitial` | 页面本身就是访问拦截页（「环境异常」「完成验证后即可继续访问」） | 同上，如实报告 |
| `attachment_access_restricted` | 附件下载页有验证码 | 正文与候选仍保留；证据链只覆盖正文 |
| `attachment_fetch_*` / `attachment_limit_reached` | 附件抓取失败/超量 | 候选保留，附件缺口在 `warnings` 里 |
| `empty_feed_or_layout_changed` | 列表页没有可识别条目或改版 | 检查来源 `config` 的 `list_selector` / `article_url_pattern` |
| `source_busy` | 另一次采集持有该源租约（1 小时） | 等租约释放；不要并发跑同一数据目录 |
| `source_disabled_or_missing` | 来源被停用或不存在 | 检查来源状态 |
| `feed_https_required` | 授权 feed 的 origin 不是 https 且不在受信网关（本机中继默认如此） | **跳过**，不是失败：开中继（`deploy/operator/relay.py serve`）或按第 2 节把该 origin 加进受信网关 |
| `feed_mode_not_enabled` / `feed_mode_requires_feed_adapter` / `feed_authorization_not_verified` | 授权 feed 的模式没开、适配器不对或授权依据未记录 | 同上：这是「通道没配好」，计入跳过 |
| `feed_fulltext_missing` / `invalid_feed_content` / `feed_receipt_missing_or_changed` / `feed_original_*` | feed **已启用**但内容坏、收据对不上、原文链接不合规 | **失败**（退 1）：中继在服务坏内容，去查 relay 的 staged 内容 |
| 其它类名（如 `KeyError`） | 代码异常 | 记录并反馈；脚本不会静默跳过 |

### 退出码怎么读：跳过 / 失败 / 降级（第 22 轮收敛）

一条无人值守的命令必须能从退出码看出「今天有没有事」，所以三类结果分得很清楚
（判定只看**错误码与连接结果**，从不看来源名字）：

| 结果 | 判据（可解释证据） | 退出码 | 打印 |
| --- | --- | --- | --- |
| **跳过** | 该源的所有错误码都属于「可选通道未启用」家族：`feed_https_required`、`feed_mode_not_enabled`、`feed_mode_requires_feed_adapter`、`feed_authorization_not_verified`。这些都是**抓取之前**抛出的配置事实（中继没开、origin 没进受信网关、授权依据没记录） | 不计入失败 | `跳过来源  <名字>：可选来源未启用（<错误码>）：通道没开，不是故障` |
| **失败** | 源本身取不到（`dns_failed`、`fetch_transport_failed`、`upstream_http_4xx/5xx`、`unsafe_address`、`unsafe_url`），或**正文**解析失败，或抽取失败 | **1** | `失败来源  <名字>：真实失败（<错误码>）` |
| **降级** | 设计内的拒绝：`article_redirected_off_pattern`、`html_access_interstitial`、`attachment_access_restricted`、附件解析失败（如老 `.doc`）。通知本身已采到，产品只是不绕过验证码 | 0 | `降级来源  <名字>：设计内的降级（<错误码>）：不绕过，通知本身已采到` |

退出码约定（与调度器的契约）：

- `0` —— 三段都跑了，且没有任何需要人管的事。**「跳过」不算事**：中继没开不是故障，
  否则命令天天退 1，警铃就失去意义。
- `1` —— 跑完了但有真失败（见上表「失败」一行）。后续段落照常执行：一个官网打不开，
  绝不能让昨天已过期的事项留在学生清单里。
- `2` —— 命令本身不可用（参数错、数据目录被拒、日期格式错）。什么都没动。

边界（诚实说明）：如果**所有**来源都被「设计内拒绝」挡住（全部 blocked/degraded，
没有任何一个源成功、也没有新快照），当前实现仍然退出码 0 —— 这与第 22 轮之前一致，
本轮**没有**改这条；队列为空时请看一眼输出里的「需授权/降级」两行。

### 「首轮 oaa 全失败」是预期，不是坏数据

`天津大学教务处` 的若干栏目列表里混有大量公众号文章。如果某源发现的前 8 条
恰好全是微信链接，抓取层会逐条拒绝为 `article_redirected_off_pattern`，
该源本轮被标成 `blocked`（设计内拒绝，**不计入失败、不改变退出码**），
报告里仍逐条列出该源与错误码。失败条目会退避，**下一轮优先抓取积压里的
官网文章**，源会自行恢复。这正是第 14 轮 t5 的目的：宁可拒绝，也不让
「环境异常」这类反爬页挤占人工复核队列。

---

## 6. 数据目录与证据卫生

- `data/` 是**随发布包出厂的公开样本**：脚本检测到 `--data-dir` 指向它（或其子目录）
  会直接拒绝，退出码 2。默认运营目录是 `data-operator/`。
- `data-operator/` 与 `.campus*` 不进发布包、不进 git（见 `.gitignore` 与
  `tools/build_release_manifest.py`）。
- **临时/验收数据目录**请放在 `.test-temp-*/`（manifest 忽略该前缀）或系统临时目录；
  不要在仓库根新建 `.t6-accept/` 这类名字——`build_release_manifest.py` 不忽略它，
  里面的 `.server-secret` 会命中 `REFUSED_NAMES`，**直接拒发整包**。
  `.campus-t6-real/` 这类 `.campus*` 目录虽然被 manifest 忽略，也建议用完即删。
- 脚本输出只打印**仓库相对路径**（目录在仓库外时只打印目录名），
  因此 `verification/` 下的运行日志可以直接随发布包出厂，不泄露本机路径。
- 证据文件：`verification/real-collect-init-<日期>.log/.json`（首次真实初始化，
  19 源 → 181 快照 / 96 候选 / 96 待复核）、
  `verification/real-collect-second-<日期>.log/.json`（同一来源第二次运行：
  新增快照 0、新候选 0、合计仍 7，证明不会重复入库）。

---

## 7. 已知未验证（诚实清单）

1. 只在「代理 TUN + fake-IP + 受信网关」这一种网络形态下实跑过；未在真正可直连
   `*.tju.edu.cn` 的校园网环境验证（该环境应无需受信网关，逻辑上等价）。
2. 未连续多日运行，因此「oaa 微信积压耗尽后源恢复健康」只有队列行为的推断和
   单次重跑证据，没有多日趋势数据。
3. 未验证附件验证码页（`download.jsp`）在人工浏览器中打开后的可读性；
   产品按「附件受限」处理，不影响正文候选。
4. 未在 PostgreSQL 上运行本脚本（本地运营实例使用 SQLite）；数据库 URL 由
   `CAMPUS_DATABASE_URL` 继承，未专门验证。
5. 未量化每源 8 篇/天在 19 个源上的长期数据增长与磁盘占用。
6. 计划任务**没有真的重启电脑验证过**。证据是注册项本身：`LogonType=S4U`
   （不依赖登录会话，任务由任务计划服务在会话 0 里以该用户身份运行）、
   `CalendarTrigger` 每日触发、任务存在任务计划库（`\TouxianCampusDaily`，随机器持久）。
   要亲眼确认，重启后跑一次 `powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status`。
7. 安装/卸载的「权限不足」分支**没有实跑过**（本机验证会话本身就是管理员），
   只验证了它会以退出码 3 和可诊断文案结束。
8. 未验证机器处于「睡眠」而非关机时的行为：`schtasks` 的错过策略只在关机/重启场景下确定，
   睡眠唤醒后是否补跑取决于电源设置，未实测。
