# 真实可用指引：从零到学生能用

> 适用对象：接手运营「偷闲·天大版」的人（或 AI）。
> 目标：**在真实官方页面上跑出学生能看、能用的通知清单**，并把每天要做的动作固化成命令。
>
> 本文只写**实测过**的行为。所有数字都来自第 14 轮的真实运行，命令可复跑。
> 相关文档：仓库入口 `AGENTS.md`、采集细节 `docs/REAL-DATA-RUNBOOK.md`、
> 缺陷诊断 `docs/DIAGNOSIS-REAL-USE.md`、中继契约 `deploy/operator/OPERATIONS.md`。

---

## 1. 这套东西到底能干什么

它把天津大学各官网/公众号的通知，变成学生能直接照做的事项：

| 学生看到的 | 从哪来 | 硬约束 |
| --- | --- | --- |
| 标题 | 文章元数据/正文标题（不是栏目标题） | 栏目名（如「培养工作」）会被识别并排除 |
| 什么时候（截止） | 原文里逐字匹配到的日期/时刻 | 引文必须能在原文里核对；推不出的就标「待确认」，**不猜** |
| 需要做什么 | 原文里的祈使句 | 无依据不生成 |
| 准备哪些材料 | 原文明确要求提交的单据/材料 | 举例性提及不算要求 |
| 办理入口 | 原文/附件里的链接 | 非全球地址、反爬页一律拒绝 |
| 原文依据 | 逐字引文 + 快照 + content_hash | 可机器复核，见第 6 节 |

**不做的事**：不伪造通知、不绕过微信反爬、不放宽地址安全判定、不把相对时间
（「三天内」）悄悄锚定到发布时间。

---

## 2. 五分钟跑起来（本机 operator 模式）

这是运营者最常用的形态：本机起服务、本机采集、人工复核后学生可见。

```powershell
# 0) 仓库根目录，$PY 是装齐依赖的解释器
# 1) 初始化数据目录并做第一次真实采集（19 个官方源）
& $PY scripts/campus_collect.py init --data-dir data-operator --gateway-from-config

# 2) 起运营实例（只绑 127.0.0.1）
$env:CAMPUS_CLOUD_MODE='operator'
$env:CAMPUS_DATA_DIR=(Resolve-Path data-operator)
$env:CAMPUS_OPERATOR_HOST='127.0.0.1'
$env:PORT='8137'
& $PY serve_cloud.py
```

打开 `http://127.0.0.1:8137/`：学生端就是「待办清单 / 找信息 / 我的事项 / 我的设置」；
运营端在管理工作台里做复核与来源管理。

> `--gateway-from-config` 只把 `config/campus-sources.json` 里已声明的官方 origin
> 填进 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`（仅本进程）。本机代理 TUN 会把
> `*.tju.edu.cn` 解析成 `198.18.x.x`（非全球地址），`SafeFetcher` 拒绝它们是**对的**
> ——那是防 SSRF 的。不要为了「让网络通」去改这个判定。

> 日常运营只要一条命令（采集 → 复核发布 → 下架），见 3.0 节；要每天自动跑就装 Windows 计划任务，
> 安装与排障也在 3.0 节。下面 3.1–3.5 是分段排查时用的。

---

## 3. 每天要做的三件事

### 3.0 一条命令（日常就用这个）

三段（采集 → 复核发布 → 下架）已经串成一条命令，无人值守也能跑：

```powershell
# 演练（默认）：只读，不改任何数据
& $PY scripts/daily_update.py --data-dir data-operator --gateway-from-config

# 真正执行（写库）：计划任务跑的就是这一条
& $PY scripts/daily_update.py --apply --data-dir data-operator --gateway-from-config `
  --json-out data-operator/logs/daily-manual.json
```

- 退出码：`0` 全部正常；`1` 有失败但未阻断（部分来源失败、复核有拒绝）；`2` 用法错误。
  **`1` 不等于白跑**：跑通的段落已经落库，日志逐段写明。
- 同一个数据目录只允许一个实例（文件锁 `data-operator/.daily-update.lock`），后到者立刻退出并说明原因，
  不排队、不并行写库。
- 实测（2026-10-21，仓库自己的 `data-operator` 只读副本，2 个全新目录副本各跑一遍）：
  第 1 次「新增快照 160 · 新候选 109 → 接受 7 · 拒绝 44 · 留给人 58 → 发布 7」，退出码 0；
  第 2 次「新增快照 26 · 新候选 15 → 接受 2 · 拒绝 60 → 发布 3（已发布过的不会重复计入）」，退出码 0；
  两次之后公开列表仍 7 条、0 个重复标题。坏来源注入后该源报 `unsafe_address`、其余 19 个来源照常、
  退出码 1（有失败但未阻断），已有数据只增不毁。明细见 `verification/coldstart-t13/`。

**装在 Windows 计划任务里每天自动跑**（需要管理员 PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install         # 默认每天 07:30
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Install -Time 08:15
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status          # 状态 + 最近一次执行摘要
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -RunNow          # 立刻跑一次
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Uninstall       # 卸载（日志保留）
```

装的注册项（本机实测注册过并真跑过一次）：任务名 `TouxianCampusDaily`、每天 07:30、`LogonType=S4U`
（**不依赖登录会话**）、`MultipleInstancesPolicy=IgnoreNew`（不并发）、`StartWhenAvailable=False`
（**关机那天不补跑**）。解释器要真的 `import fastapi, bs4, sqlalchemy` 成功才会被采用，被跳过的候选
连同原因一起打印。日志在 `data-operator/logs/`：`daily-YYYYMMDD.log`（完整输出）、
`daily-YYYYMMDD.json`（机器可读摘要）、`last-run.json`（最近一次的结果码），`-Status` 读的就是这三样。
改时间/临时停用/看日志的完整说明见 `docs/REAL-DATA-RUNBOOK.md` §4.2、§4.3。

### 3.1 采集

```powershell
& $PY scripts/campus_collect.py collect --data-dir data-operator --gateway-from-config
```

幂等，**可以随便重跑**。实测（`--source 奖助`，全新目录连跑两次）：

| | 新增快照 | 新候选 | 候选合计 |
| --- | --- | --- | --- |
| 第 1 次 | 7 | 7 | 7 |
| 第 2 次 | 0 | 0 | 7 |

输出会如实报告降级源与错误码，例如一次真实运行：

```
来源 2 个：成功 0 · 降级 2 · 失败 0
入库 新增快照 25 个 · 文档总数 25
候选 新增 13 · 合计 13 · 待复核 13
错误码 attachment_access_restricted×12, unsafe_address×4
积压 待抓 7 · 失败待重试 8（每次每源最多处理 8 篇，后续每天自然补齐）
```

**看到「降级」不是坏了**：说明该源部分页面取不到（附件要登录、地址非全球），
已抓到的仍然可用，取不到的会在后续每天的运行里自然补齐。

### 3.2 人工复核

复核是产品的一部分，不是可选项：**没有人工接受，任何通知都不会对学生公开**。

- 网页：管理工作台 → 复核队列，逐条看「什么时候/做什么/材料/入口/原文依据」，接受或拒绝。
- 脚本/接口：`GET /api/v1/admin/reviews` 拿候选，主键是 `candidate_id`（**不是** `id`）；
  决策 `POST /api/v1/admin/reviews/{candidate_id}/decision`，body 里必须带
  `expected_candidate_revision`（并发保护）；写请求还要同时带 `X-CSRF-Token` 和
  `Idempotency-Key`，缺一个就是 422 `idempotency_required`。

复核时最该看的是「原文依据」：每条字段都挂着引文 id，点开能看逐字引用与快照。

### 3.3 发布与自查

接受候选后由 `core.events.reconcile_candidate()` 生成事项版本才对外可见。
自查一条真实通知长什么样（实测输出）：

```
标题: 天津大学智能与计算学部2026年博士研究生招生办法
事件类型: activity

截止时间:
  - [school_deadline/date]  2025-10-20
  - [school_deadline/minute] 2026-01-15T12:00:00+08:00
  - [school_deadline/date]  2026-03-10
  - [school_deadline/minute] 2026-04-07T12:00:00+08:00

材料:
  - 招生简章中规定的所有申请材料，包括《中国高等教育学历认证报告》…

办理步骤:
  - 基本要求：符合《天津大学2026年博士研究生招生简章》规定的报名条件
  - 报名流程和要求等按照…中规定的申请流程执行
  ...
```

注意「查看原文」的地址在 `evidence[].source_url`，不在事项本身的字段里
（检索命中的 `source_url` 是摘要投影）。别去事项 top-level 找 `source_url`，那里没有。

### 3.4 批量复核与发布（可选的一条命令）

网页工作台适合逐条判断；量大的时候用脚本，它用的是同一条复核门，只是把
「接受/拒绝」按写下来的策略一次性执行，并逐条留痕：

```powershell
# 先演练：只打印结论，不写决策
& $PY scripts/review_and_publish.py --data-dir data-operator --json-out verification/review-dryrun.json

# 确认无误后写入，并把已接受候选发布成对外可见的事项
& $PY scripts/review_and_publish.py --data-dir data-operator --apply --json-out verification/review-apply.json
```

接受策略（写在脚本头部，改了要一起改文档）：拒绝栏目名/错误页/无正文依据的候选，
以及「取消通知但没有可关联原事项」的候选；其余（含名单公示、新闻）都接受——
它们没有截止时间，不会变成待办，只是可被检索到。

**接受 ≠ 发布**：决策只是复核结论，事项要经 `reconcile_candidate()` 才有版本对外可见。
脚本在 `--apply` 时会自动做这一步；如果你想交给 operator 实例的 worker 做，加
`--no-publish`。

第 14 轮真实数据实测：185 条候选 → 接受 181、拒绝 4 → 发布 181 条事项，
其中 90 条带截止时间、29 条带材料、101 条带办理步骤，80 条判定「需要行动」。

### 3.5 抽取规则升级后必须重抽

`campus-collect.py reextract` 把库里的旧规则候选按当前规则重跑一遍
（不联网、不删数据），重抽后的候选需要**重新人工复核**：

```powershell
& $PY scripts/campus_collect.py reextract --data-dir data-operator
# 需要只针对某几条时（标题片段或 candidate_id）：
& $PY scripts/campus_collect.py reextract --data-dir data-operator --source 雅思考试
```

> 机制：候选按「输入 + 规则版本」定键，所以**同版本重抽是幂等的（不产生新版本）**。
> 改了抽取规则就必须升 `EXTRACTOR_VERSION`，否则旧候选不会被识别为过期。

---

## 4. 一条命令验收整条链路

```powershell
& $PY scripts/verify_real_use.py --source 智能与计算 --source 培养办事流程 --limit 4
```

它会真实抓取 → 解析 → 抽取 → 用真实鉴权/CSRF/幂等调复核接口接受 → 发布 → 检索 →
读事项详情，每级打印 PASS/FAIL，最后给汇总。第 14 轮实测：

```
来源 2 个，入库 25 篇，候选 8 条，已接受 4 条，已发布 4 条，检索命中合计 12，带结构化字段的事项 3 条
EXITCODE=0
```

退出码 0 才算通过。`body=0` 的候选会标「产品按设计拒绝空正文」，那是正确行为
（图片正文/空正文不允许凭空生成内容），不是脚本失败。

---

## 5. 三种部署形态怎么选

| 形态 | 命令/环境 | 谁能访问 | 开发登录码 |
| --- | --- | --- | --- |
| `demo` | `CAMPUS_CLOUD_MODE=demo` | 演示/验收 | 开 |
| `operator` | `CAMPUS_CLOUD_MODE=operator`（默认绑 127.0.0.1） | 本机运营 | 开（仅本机） |
| `live` | `CAMPUS_CLOUD_MODE=live` | 公网 | **强制关闭** |

`live` 模式要么主机能路由 `*.tju.edu.cn` 直采，要么用运营方中继
（`deploy/operator/relay.py`，契约见 `deploy/operator/OPERATIONS.md`）。主机采不到
校园源时设 `CAMPUS_EMBEDDED_WORKER=false`，由外部每日任务跑采集。

**部署前必须做的三件事**：跑出厂门（第 7 节）、确认没有活凭证进包
（`.server-secret` / `session.json` 会被打包器直接拒发）、确认 `live` 模式下
开发登录码确实关闭。

---

## 6. 出问题怎么判断（别猜，看信号）

| 现象 | 含义 | 怎么办 |
| --- | --- | --- |
| `unsafe_address` | 解析到非全球地址（本机代理 TUN 的 fake-IP 等） | 配 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`，**不要**关掉地址检查 |
| `attachment_access_restricted` | 附件需要登录/授权 | 合规补充：运营方中继或授权附件流程 |
| `article_redirected_off_pattern` | 链接被重定向到非文章页（如微信验证页） | 正确拒绝，不要绕过；该来源需授权订阅通道 |
| `html_access_interstitial` | 抓到的就是访问拦截页（「环境异常」等） | 同上，识别即拒绝 |
| 解析 `failed` + `html_empty_body_requires_review` | 正文是图片/空 | 需 OCR 或人工补录，禁止凭空生成 |
| 事项 `deadlines` 里 `precision=unknown` | 原文没给出可判定精度 | 正常：请学生回原文核对，不要自动补 23:59 |
| 复核 409 `revision_conflict` | 别人已改过该候选 | 重新拉队列用最新 `revision` 再决策 |

---

## 7. 出厂门（改完代码必须过）

```powershell
$bt = "$PWD\.test-temp-rN\basetemp"
New-Item -ItemType Directory -Force -Path $bt | Out-Null   # ← 父目录必须先建好
& $PY -m pytest server/campus/tests -q --junitxml=verification/backend.xml --basetemp=$bt
```

第 14 轮结果：**758 passed / 8 skipped / 0 failed / 130 subtests**，
junit `tests=896 failures=0 errors=0 skipped=8`，exit 0。

> **坑（真实踩过）**：`--basetemp` 指向的目录父级不存在时，pytest 会在 setup 阶段
> 抛 `FileNotFoundError`，结果是 `errors=169` 而不是失败用例，而且会把这个坏档写进
> `verification/backend.xml`。看到 `errors>0` 先查 basetemp 父目录。

打包：

```powershell
& $PY tools/build_release_manifest.py          # 当前 814 文件
& $PY tools/package_release.py --root . --out ../campus-release.zip
```

---

## 8. 能力边界（诚实清单）

**已验证**（每条都能在本次交付里找到出处；未标出处的见 `VERIFICATION.json` 的 `round_*` 小节）

- 2 个官方源、25 篇真实文档的完整链路：采集 → 解析 → 抽取 → 复核 → 发布 → 检索 → 事项详情。
- 采集幂等（重跑不重复入库）：同源第二次 0 快照 / 0 候选。
- 微信反爬页被识别并拒绝（`环境异常` / `wappoc_appmsgcaptcha`）。
- 标题不再取栏目名（gs 模板「培养工作」已修）；`招生/报名/推免/招录` 归入 activity。
- **日常一条命令**采集→复核→下架（3.0 节），已在 2026-10-21 的两个全新数据目录副本上各跑两次：
  公开列表 7 → 7 条、0 个重复标题、库里 `events` 正好 7 条；坏来源注入后其余 19 个来源照常、数据只增不毁
  （`verification/coldstart-t13/`）。
- **每天自动跑**已挂成 Windows 计划任务并真跑过：`-Status` 退出码 0、调度器触发两次都落盘
  `daily-YYYYMMDD.log` / `.json` / `last-run.json`，重叠时后到者 1 秒内退出并写明锁
  （`tools/install_daily_task.ps1`，说明见 `docs/REAL-DATA-RUNBOOK.md` §4.2）。
- **手机端**：360/390/430 三档宽度五个页面真实浏览器逐页走通，`scrollWidth == innerWidth`、
  触控目标 0 个 <40×40；另一名队友独立复核（自起服务、自写探针、自己点击）结论一致
  （`verification/mobile/`）。
- **首屏**：匿名首页第一张卡显示的是**未来**期限（2026-10-22），前 8 张 0 个过期日期；
  360×640 / 375×667 与真机可视高度 360×536 / 375×553 都有带截止时间的卡片在首屏内
  （t14 独立复验，见 `VERIFICATION.json` 的 `round_18_first_screen`）。
- **冷启动**：从发布包解压到全新目录、照文档启动、登录、看到真实通知、加入待办、真实杀进程重启后待办仍在；
  注入坏来源后报错可读且不破坏数据（`verification/coldstart-t13/README.md`）。
- **登录可用**：启动脚本的单机档补齐 dev_auth/数据目录/管理员邮箱，实测能拿到验证码并登录
  （`server/campus/tests/test_launcher_login.py`）。
- 上一轮结束时的自动化测试覆盖上述行为；本轮的计数见第 7 节（数字取自 `verification/backend.xml`）。

**未验证 / 已知限制**（这份清单只增不减；后来补验的标注「已补验」但不删除）

1. 19 个源**没有逐条人工抽检**：全量采集跑过（20 个注册来源：成功 17 · 降级 2 · 失败 1），
   但没有对全部已发布事项逐条复核抽取质量。**已补验**：全量真的跑过；**仍未验证**：逐条抽检。
2. 没有跨日/长期运行观察。**已补验**：定时任务真的注册并被调度器触发过（本机 2026-10-21 00:29 一次）；
   **仍未验证**：没有真的重启机器（只有 S4U 注册项作为证据）、非管理员会话的权限分支没实跑、
   睡眠/唤醒的补跑行为没测。
3. 合法公众号 feed 能否正常收录未验证（本机没有已授权的公众号通道）；
   已验证的只有「反爬页会被拒绝」与运营方中继这条人工通道。**仍未验证**：任何自动化的公众号收录。
4. 前端浏览器验证。**已补验**（t11/t12/t15/t17 + 本任务）：360/390/430 与 360×640/375×667 模拟视口、
   真实点击走查、独立复核。**仍未验证**：真机 iOS/Android（只验了桌面 Edge 的移动视口仿真）、
   管理工作台深层页面未逐页走、OCR 截图识别未在手机上验、
   `src/campus/tests/*.e2e.mjs` 那套 JS 端到端用例没有整套跑过（需要浏览器 runner）。
5. 截止时间引文在「日期藏在长括号里」的句子上仍可能较长（自校验要求引文能独立
   推出同一条时间，做不到就加宽）。可核对性优先于简短。**仍未验证**：人工抽检 30 条样本。
6. 部分真实通知的截止日期已过（采集到的是 2025–2026 学年通知），学生端会把它们
   归到「已截止」，这是数据现实，不是缺陷。
7. 推送/邮件投递在本实例未配置（`available=false`），所以投递链路未端到端验证。
8. 发布包里的历史公开样例是 2026-10-09 的快照，期限基本已过：它只用来熟悉界面，
   想看能办的事必须接实时来源（见第 3 节）。

---

## 9. 下一轮最值得做的三件事

1. **逐条抽检已发布事项的抽取质量**（标题/期限/材料/入口），把错的挑出来——全量采集已经跑过，
   缺的是逐条人工核对，不是跑不动。
2. **真机验证**：iOS/Android 各一台，走「登录 → 今天 → 详情 → 待办」，确认移动端视口仿真之外的差异
   （安全区、真机可视高度、触控、以及 `--basetemp` 之外的环境差异）。
3. **把 `src/campus/tests/*.e2e.mjs` 接进常规门**：现在每次只跑单测 36 例；
   把「默认视图给内容」「首屏有带截止时间的事项」这类断言做成红测，改前端时才有保护。

---

## 10. 备份与恢复

数据全在**数据目录**里（operator 档默认 `data-operator/`），发布包里**没有**它——备份就是备份这个目录：

```powershell
# 1) 先停服务，并确认没有正在跑的日常更新（锁文件不存在）
Test-Path data-operator\.daily-update.lock        # 期望 False
schtasks /Query /TN TouxianCampusDaily            # 装了定时任务的话，确认它当前没在跑

# 2) 整目录复制到一个带日期的位置
Copy-Item data-operator (Join-Path $env:USERPROFILE ('campus-backup-' + (Get-Date -Format yyyyMMdd))) -Recurse
```

- 目录里的关键东西：`campus.sqlite3`（**连着 `-wal` / `-shm` 一起拷**）、`snapshots/`（原文快照，
  证据链要用）、`.server-secret`（签名密钥；丢了旧的日历订阅/快照链接会失效）、`logs/`（每日更新日志）。
- **实测**：整目录复制出来的副本可以直接当数据目录用：本任务与冷启动验收都这样做过——
  复制 `data-operator` → `CAMPUS_DATA_DIR` 指过去 → `python serve_cloud.py` 正常起来，
  登录、待办、已发布通知都在（`verification/coldstart-t13/README.md`）。
- 恢复：把副本放回原处、用 `CAMPUS_DATA_DIR` 指向它即可；**不要**在服务运行时替换 `campus.sqlite3`。
- 公网部署用的干净快照（已擦除隐私集合）另存于 `deploy/snapshot/`，随发布包出厂，与运营数据无关。
