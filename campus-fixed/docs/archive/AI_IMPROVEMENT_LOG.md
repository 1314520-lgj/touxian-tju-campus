# AI_IMPROVEMENT_LOG —— 逐轮记录

> 每轮追加一节，**不覆盖历史**。格式固定：发现的问题 / 证据 / 方案 / 改动的文件 /
> 测试结果 / 实际改善 / 未解决的风险 / 下轮建议。
>
> 规则：**无法验证的部分必须如实写出**，不许用「应该没问题」代替证据。

---

## 第 1 轮 —— 项目基线、风险识别与截止日期正确性（2026-10-11）

输入：`偷闲天大版-2.0-品位版.zip`（解压后仓库根仍名 `偷闲天大版-1.8满分版`）。
这是长期自主循环的**起点**：本轮先建立基线与任务池，再完成第一个可安全验证的高价值改进。

### 一、基线（全部实测，可复现）

| 项 | 结果 |
|---|---|
| 后端全量 | **587 passed / 8 skipped / 0 failed / 106 subtests**，exit 0，259.94s（JUnit `tests=701`） |
| 前端单元 | **33 passed / 0 failed** |
| 发布清单 | 随包 `RELEASE_MANIFEST.json` 记录 **752** 项，与交付 ZIP 的 753 个条目（752 文件 + 清单自身）**逐条比对零差异**；但 `源码导航.md` 与 `VERIFICATION.json` 都写 751（见 P1-3） |
| 版本控制 | 仓库**没有** `.git`，本轮新建；基线提交 `2319236` |

基线数字与随包 `VERIFICATION.json` 记录的完全一致，说明输入包是自洽的。

### 二、发现的问题

#### P0-1 数字日期的 30、31 日被截断成 3 日（**静默返回错误日期**）

**证据（随包真实数据）**：候选 `3c215803…` 的截止时间里，
原文 `毕业时间：2026.09.01-2027.08.31` 被抽取为 **`date=2027-08-03`**，差 28 天，
且以 `precision='date'` 返回 —— 格式合法、内容错误，正是本项目不变量第 11 条点名最危险的一类。

**根因**：`server/campus/date_patterns.py` 的数字年模式
`(?P<year>20\d{2})[./-](?P<month>…)[./-](?P<day>[0-2]?\d|3[01])`
的日备选**先**匹配 `[0-2]?\d`。对 `30` 而言 `[0-2]?` 匹配空、`\d` 匹配 `3`，
匹配就此成功；`3[01]` 永远轮不到。又因为**没有尾边界**，`2026.11.30` 于是被读成 `2026.11.3`。
同一个文件里**无年**的那条模式一直带着 `(?!\d)`，所以只有数字年这条出错。

影响面：`YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD` 三种写法，日为 30 或 31 时全部中招。

**方案**：给数字年模式补上与无年模式一致的尾边界 `(?!\d)`。引擎回溯后
`[0-2]?\d` 因后继是数字而失败，转而用 `3[01]` 匹配 `30`/`31`。

#### P1-2 `24:00` 被当成不可能的时刻，连日期一起丢掉

**证据**：`申请截止日期为11月30日24:00`、`请于11月30日24点前提交`、
`报名截止时间：2026年11月30日 24:00` 全部落到 `precision='unknown'`、`date=None`。
通知明明写着 11月30日，学生看到的是「截止时间待确认」。

**根因**：`server/campus/content.py::_deadline_row` 的
`if hour is not None and (hour > 23 or minute > 59): value = None`。
对 `25:00` 这类**真正不可能**的时刻，丢弃是对的；但 `24:00` 是中文里表示「当天结束」的
标准写法，等价于次日 00:00，不是不可能的时刻。

**方案**：只对 `24:00` 特判 —— 日期顺延一天、时刻归零（`minute` 精度）。
其余越界时刻（`24:30`、`25:00`）**照旧拒绝**，仍然不猜。

#### P1-3 / P1-4 交付文档的数字与描述滞后于代码

- `源码导航.md` 与 `VERIFICATION.json` 写「751 个交付文件」，而清单实测 **752**。
  用交付 ZIP 逐条核对过：ZIP 753 个条目 = 752 个文件 + 清单自身，**零差异** ——
  也就是说**错的是文档**，清单本身是准的，不是清单滞后。
  （第 1 轮新增 4 份维护文档、`tools/ai_round.py` 与一个测试文件后，清单重建为 **758** 项。）
- `源码导航.md` 称 `11.30 截止` 之类的紧凑写法「没有覆盖」—— 实测 `11.30`、`11/30`、`11-30`
  都能正确解析。
- 同一节称 `trafilatura`「没有固定版本、没有 lock」—— 实际
  `requirements-campus.lock.txt` 里是 `trafilatura==2.3.1`。

这三条都不影响运行，但**它们让读者按错误的前提做决策**，属于要修的一类。

#### P1-5 发布校验器**读不了自己交付的压缩包**（把好包判成坏包）

**发现方式**：本轮要重建清单、重新打包，于是按文档里的命令校验交付 ZIP —— 结果 `FAIL`。

**证据（先复现）**：
```
python tools/build_release_manifest.py --root <交付清单> --verify 偷闲天大版-2.0-品位版.zip
FAIL: 2 problem(s)
  - unexpected entry in archive: σü╖Θù▓σñ⌐σñºτëê-1.8µ╗íσêåτëê/µ║Éτáüσ»╝Φê¬.md
  - 1 manifest file(s) absent from archive: 源码导航.md
```
**同一个文件同时被判为「多出来的」和「缺失的」**，退出码 1。

**根因**：ZIP 条目的名字以字节存储，**是否 UTF-8 只由通用标志位 bit 0x800 声明**。
Python 的 `zipfile` 在写非 ASCII 名时会置该位，所以**本项目自己打的包一直没问题**；
而这份交付 ZIP 是别的工具打的，没置位 —— `zipfile` 于是按 cp437 解码，
`源码导航.md` 变成 `µ║Éτáüσ»╝Φê¬.md`，与清单里的真名对不上。
`verify()` 直接把 `info.filename` 当名字用，没有处理这种「未声明编码」的包。

**为什么以前没暴露**：1.9 的交付包是 `tools/package_release.py` 打的（置位），
所以文档里那条「校验 751 个文件全 OK」是真的；这份包换了打包工具，缺陷才浮出来。

**影响**：**完好的包被自己的工具判为失败**。用户照着文档敲命令会得到「校验不通过」，
于是去怀疑、甚至去「修」一个本来没坏的东西 —— 这比不提供校验更糟。

**方案**：新增 `archive_entry_name()`：置位则原样返回；未置位则把字节按 cp437 还原后**再按 UTF-8 解码**，
解不出来就保持 `zipfile` 的结果（真正是 cp437 的包不受影响）。

**验证**：同一条命令现在输出 `OK: 752 files verified`，退出码 0。
新增 `server/campus/tests/test_release_manifest_verify.py` **6 例**：
把 fix 撤掉时**恰好 1 例失败**，真实 ZIP 同时复现那两条错误；恢复后 6 例全过。

### 三、改动的文件

| 文件 | 改动 |
|---|---|
| `server/campus/date_patterns.py` | 数字年日期模式补 `(?!\d)`，附根因注释 |
| `server/campus/content.py` | `_deadline_row` 特判 `24:00` → 次日 00:00 |
| `server/campus/tests/test_extraction_accuracy.py` | **新增 10 例**（5 个参数化 + 5 个独立用例），含 2 例「仍然拒绝猜测」的反向保证 |
| `tools/build_release_manifest.py` | 新增 `archive_entry_name()`，处理未声明 UTF-8 标志的压缩包 |
| `server/campus/tests/test_release_manifest_verify.py` | **新建，6 例**：未置 UTF-8 位的包必须通过校验；置位的包也必须通过；坏包仍被拒绝；非 UTF-8 名不被误改；ASCII 名不变 |
| `AI_PROJECT_CONTEXT.md` `AI_IMPROVEMENT_PLAN.md` `AI_PENDING_TASKS.md` `AI_IMPROVEMENT_LOG.md` | 新建（长期上下文 / 方向 / 任务池 / 本记录） |
| `tools/ai_round.py` | 新建：一轮验证的**单一入口**，供调度器调用 |
| `源码导航.md` `VERIFICATION.json` | 修正上述滞后数字与描述；文件数 → **758**，后端计数 → **603 / JUnit 717**，并补记本轮日期修复与发布校验器修复 |
| `RELEASE_MANIFEST.json` | 重建（纳入 4 份维护文档、`tools/ai_round.py` 与新增测试，刷新两个证据文件哈希） |

### 四、测试结果

先写复现（红）：新增 10 例中 **8 例失败**，失败原因与预期一致
（`unknown` ≠ `minute`、`2027-08-03` ≠ `2027-08-31`），另 2 例「不可能时刻仍拒绝」本就通过。

修复后（全部改动就位，一轮跑完）：

```
603 passed, 8 skipped, 1 warning, 106 subtests passed in 419.26s (0:06:59)   exit 0
```

JUnit `tests=717`、611 条 testcase、8 条 skipped、`failures=0 errors=0`。
587（基线）+ 16（本轮新增：10 例日期 + 6 例发布校验）= 603，**数目对得上**。

路径门禁：`normalize_evidence_paths.py --check` → `0 file(s), 0 path(s)`。
本轮首次跑到门禁报红（本轮新跑的证据里，pytest 的 warning 摘要带出了解释器绝对路径），
按文档流程执行一次规范化后归零 —— 这正是门禁该有的行为，不是产品缺陷。

浏览器验收抽查：`a11y` 入口 17 项断言全过（本轮新增的 `tools/ai_round.py` 自动起服务）。

### 五、实际改善

- `毕业时间：2026.09.01-2027.08.31` → **`2027-08-31`**（原来 `2027-08-03`）。
- `11月30日24:00` → **`2026-12-01T00:00:00+08:00`**（原来「截止时间待确认」）。
- 回归检查了 17 个此前**已经正确**的写法（`11月30日前`、`11.30`、`2026年11月30日`、
  `中午12点`、跨年区间 `2026年12月25日至1月5日` 等），结果**全部不变**。
- 2 例反向保证确认：`24:30`、`25:00` 仍然报 `unknown`，没有因为这次放宽而开始猜。
- 发布校验器：对交付 ZIP 的校验由 `FAIL / 2 problems / exit 1` 变为
  **`OK: 752 files verified / exit 0`** —— 交付包第一次能被本项目自己的工具正确校验。

### 六、本轮在**我自己的工具**里发现并修掉的缺陷

`tools/ai_round.py` 是本轮新写的，第一次运行就暴露了两个真问题，都已复现并修复：

1. **子进程没有收到改过的环境变量**：`run()` 没把 `env` 传给 `subprocess.run`，
   于是 e2e 入口读不到 `CAMPUS_E2E_URL`，退回它自己的默认端口 8015，
   报 `ECONNREFUSED` —— 看起来像服务起不来，实际是参数没传进去。
2. **证据日志没有被刷新**：原先只在子进程结束后写日志，导致
   `verification/audit/backend-suite.log` 一直是**上一轮**的内容。
   而 `test_evidence_tree_hygiene.py` 是在**本轮套件内部**读这个文件的，
   于是它读到上一轮的失败文本并报红 —— 一个指向「已经修好的文件」的假失败。
   改为**边跑边流式写入**，套件内部读到的就是本轮内容。

> 这两条印证了同一条经验：**新工具要真的跑一遍才算写过**。

### 七、未解决的风险 / 未验证的部分

- **8 条 PostgreSQL 用例仍跳过**（本机无隔离 PostgreSQL），这是**未验证**，不是通过。
- 容器镜像未构建（本机无 docker/podman）；未接真实 Meilisearch / 云服务 / SMTP / 生产队列。
- 修复只改了**抽取规则**，**没有**回填随包 `data/` 里已经存下的错误值
  （`2027-08-03` 仍在库中）。这是**有意**的：回填等于改写已复核的历史数据，
  属于需要人工批准的 C 级操作。已记入任务池。
- `24:00` 与 `24时` 现在落点不同（后者是 `date` 精度），语义等价但显示不同 —— 已记入任务池。
- 随包数据里 `24:00` 出现 **0 次**，所以 T2 的收益是**面向未来通知**的，不是修复现存错误。
  T1 则**确实**命中了现存数据（`2027-08-03`）。
- 校验器**只修了名字解码**：条目数据损坏时仍以未捕获的 `zipfile.BadZipFile` 抛出，
  而不是打印一行 FAIL。包仍被拒绝（退出码非零、且不写 `.sha256` 旁文件），
  所以这是**体验问题不是正确性问题** —— 已记入任务池 T15，本轮不动它，
  免得把「本轮只改名字解码」这件事说成已经顺手修完了。
- 9 个浏览器入口本轮**只抽查了 `a11y` 一条**，其余 8 条沿用 1.9 的记录，
  属**未在本轮复验**。这正是 T7 要解决的事。
- 本轮重新打了包（`tools/package_release.py`），因为校验器刚修好，
  正好用一次真实打包把「写包 → 立刻回调校验器 → 通过才写 `.sha256`」这条链走通；
  但**9 个浏览器入口没有全量复验**，所以这个包在「浏览器端到端」这一维度上
  继承的是 1.9 的证据，不是本轮的。

### 八、环境记录（与产品无关，但每轮都会遇到）

- 沙箱对单次批量删除设阈值 → pytest 收尾会中断，必须用一次性 `--basetemp`。
- 只要**打开过** `data/campus.sqlite3`，就会留下 `-wal`/`-shm`；发布清单构建器**拒绝**打包它们。
  本轮处理：`wal_checkpoint(TRUNCATE)` 返回 `(0,0,0)`（无待写页）、`integrity_check=ok`、
  库 SHA 与清单**逐字节一致**（`629d8c61af1c…`），确认无损后才删除两个旁文件。
- 浏览器入口共用一台服务会因验证码 30 秒冷却拿到 429 —— 必须一条入口一台服务。

### 九、下轮建议

1. **T7** 用 `tools/ai_round.py --browser` 把 9 个入口全跑一遍，确认整条链路（含 `reliability`
   用的另一台服务）都能自动起停。**本轮最高优先级**：这是唯一一处「本轮没复验却写进了交付包」的地方。
2. **T6** 检查「机会」页与搜索页对**已过期但仍 `published`** 的 20 个事件是否与「今天」页口径一致。
3. **T5** 处理 `unknown` 截止时间里的**小节标题**噪声（先证明标题与正文可区分，不要简单抑制）。
4. **T15** 让校验器把损坏的包打印成一行 FAIL，而不是抛未捕获的 `BadZipFile`（体验向，低风险）。
5. 回填问题（第七节第 3 条）需要人工批准，先不动。

### 十、本轮提交（每处改动独立成提交，便于单独回滚）

| 提交 | 内容 |
|---|---|
| `a58660c` | `fix(extraction)` 30/31 日截断 + `24:00` 丢日期，含 10 例回归 |
| `c5fc10c` | `fix(release)` 校验器读不了未声明 UTF-8 的包，含 6 例回归 |
| `1c883e7` | `chore(tools)` `tools/ai_round.py` 单一入口 |
| `f7763ea` | `docs(memory)` 四份长期文档 |
| `df595c7` | `docs` 修正 751→752→758 与后端计数，`VERIFICATION.json` 补两节 |
| `a95fe6d` | `chore(release)` 重建清单（758 项）与本轮验证记录 |

基线提交 `2319236`（导入交付包）。**每一处改动单独成提交**，符合「不把不相关改动混进同一提交」。

---

## 第 2 轮 —— 拉满改造：工具链、代码卫生与两个正确性修复（2026-10-13）

输入：本机桌面 `偷闲天大版-2.0-品位版-自主维护第1轮.zip`（内容 = 第 1 轮结束时的交付树）。目标由用户指定：
**用外部最佳实践把项目拉满，每轮全量回归 + 自我评价，迭代到满意为止**。外部输入经检索筛选后采纳两条：
GitHub 标准 CI/pre-commit 工作流（本项目此前两者都没有）、Astral ruff 推荐配置方式
（显式 select + per-file-ignores，规则与豁免都要有理由）。FastAPI 生产清单核对后
**确认无需新增**：CORS 显式白名单、自定义错误处理、登录限流、request_id、安全响应头
（nosniff / no-referrer / API no-store）本项目均已具备。

### 一、基线复测（与第 1 轮文档逐项一致）

后端 603 通过 / 8 跳过 / 106 子测试（321s）；前端 33 通过；`build_release_manifest.py
--verify` 对真实交付 ZIP：OK 758 files。三项都与包内文档吻合，说明输入包自洽。

### 二、T16：ruff 工程化（独立提交）

- 新增 `ruff.toml`：select F/E4/E7/E9/I/B/UP/RUF100，target py312。B008（FastAPI `Body()`
  默认参数惯例）、B023（三处循环内闭包逐点核验为同迭代立即调用）带理由豁免；
  测试目录 fixture 导入（env/notice 靠 import 注册，linter 看不见用法）与两个文件的
  sys.path 引导（E402）有 per-file 豁免。结果不再依赖机器 ambient 配置。
- 手工修复 19 处真告警：5 处未使用循环变量、1 处 raise-from、1 处可变默认参数、
  2 处 lambda、3 处盲目 assertRaises 收窄到真实异常类型（其中 sanitizer 的从
  `Exception` 收窄到 `FetchError` 时**第一版猜错成 ValueError，测试抓出来后改正**——
  这正是收窄型断言的价值）。
- **踩坑记录（对后续维护者重要）**：`ruff check --fix` 的 F401 会把「仅作 pytest
  fixture 导入」的 `env` 删掉（参数不算 usage），导致 21 个用例 fixture not found。
  已还原并加 `noqa: F401` + 理由注释；`ruff.toml` 的 tests/* 豁免里没有放 F401，
  因为这个坑值得每个人亲眼看见一次。
- 全仓 `ruff format` **独立一个提交**（1.9 轮只格式化了三个文件）：813 处紧凑分号
  写法（E701/E702）清零；CRLF/LF 按文件原样保留；三个各含 1 行游离 CRLF 的文件
  （app.py 等）统一为 LF，属于事故修复。提交前在格式化后的树上跑满回归（606 通过）。

### 三、T15：校验器不再对坏包抛栈

`verify()` 的 open 与 read 两类 `BadZipFile`，以及缺清单/坏 JSON，统一为一行
`FAIL: ...` + exit 1；旧测试里「BadZipFile 或 exit 1 都接受」的契约收紧为必须
exit 1，新增 4 例（垃圾字节、缺清单、坏 JSON、坏条目）。真实 ZIP 仍 OK 758。

### 四、T5：标题形截止时间不再伪装成第二个「待确认」

随包 75 个事件逐条核对：13 条 unknown 行全是小节标题（「2. 网上报名及截止时间」
「【招生截止时间】」「报名流程及截止时间」），其中 10 条所属通知**同 kind 已有可
解析期限**。保留它们等于告诉学生「截止待确认」——而其实有确认日期。
`_drop_heading_deadline_placeholders()` 只在同 kind 已可解析时丢弃；唯一信号是标题的
通知保留 unknown；「截止时间另行通知」类真实陈述由 `DEADLINE_CLAIM_WORDS` 排除。
提取器版本 campus-rules-1.4 → **1.5**（旧已接受候选重新提取时回到审核，已发布事项不改写）。

### 五、T9：`24时` 与 `24:00` 收敛到同一答案

此前 `11月30日24时` 落 date 精度、`11月30日24:00` 滚到次日 00:00——同一时刻两个答案。
`HOUR_SHI_PATTERN` 接受「时」记法，`(?<!小)` 与 `(?![辰间])` 挡住「24小时/24辰」类时长，
经既有 24:00 归一后两种记法逐字段相等。

### 六、T6 的结论（不改代码，属 B 级）

搜索命中的载荷不带 deadlines，前端无法判断是否截止；要与信息流页的「历史通知」折叠
一致，必须让搜索接口返回终止状态 —— 即动冻结契约 1.5.0。与 T10 同类，先出方案再实施。

### 七、测试与工程化结果

- 后端：**616 通过 / 8 跳过 / 0 失败 / 106 子测试**（第 2 轮新增 13 例红→绿）
- 前端：33 通过；`ruff check` 与 `ruff format --check` 全绿
- 新增 `.github/workflows/ci.yml`（ruff + 后端 3.12/3.13 矩阵 + 前端单测 + vite build +
  shipped parity，每次 push 都跑）与 `.pre-commit-config.yaml`（刻意不含行尾归一钩子）
- git 基线：`ee3c992` 交付原样 → `1a738ac` lint/CI → `0503dad` 全仓格式化 →
  `58af40b` T5/T9。每个提交前都有对应范围的回归证据。

### 八、仍未验证（如实）

PostgreSQL 8 例、真实浏览器验收 9 入口（下一节）、镜像构建、真实采集、
真实 SMTP 与个人网关送达——均未在本轮验证，不声称通过。

---

# 第 3 轮 —— 把浏览器验收拉满（2026-10-13）

指令是"从网上和 GitHub 找合适的方法拉满，做完自我评价，评价完再提升，重复到满意为止"。
第 1、2 轮做完了 lint/CI/校验器/抽取器与文档同步（见上文）；第 3 轮的主攻对象是 T7：
**浏览器验收 9 个入口没有一条命令的入口**——工具写好了，但两条入口的契约没接上，
"一条命令跑通"是假的。顺带按指令做了一次诚实自我评价（`SELF_EVALUATION.md`）。

## 一、先复现（红），并且红得很具体

全量首跑（`ai_round.py --browser --ocr`）：8/11 通过。两条失败各有明确报文：

- `feed-mode`：`POST /admin/sources` 422 `{"code":"unsafe_port"}`。入口要在界面上建一条
  指向夹具的来源，而 `create_source()` 依次跑 `canonical_url()` 与 `resolve_public()`：
  **非常规端口一律 unsafe_port；127.0.0.1 不是全局地址**——除非同一份
  `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 显式信任该来源。这正是
  `BROWSER_ACCEPTANCE.md` 里"服务以 CAMPUS_TRUSTED_GATEWAY_ORIGINS 信任该来源"的落地处，
  第 1 版驱动器没读出来；缺 `--feed-event-info` 还让"已授权供货证据标注采集方式"这项
  检查被静默跳过（没有 `CAMPUS_E2E_CAPTURE_EVENT` 时入口只往 pending_checks 里记一行）。
- `reliability`：`fixture server did not become ready`。`reliability_demo` 的
  `--data-dir` **必须不存在**（`Path(...).exists()` 直接 `parser.error`），而
  `mkdtemp` 恰好把这个目录建出来了；`--manifest` 与 `CAMPUS_E2E_FIXTURE` 同样缺失。

## 二、修复与复跑（绿）

驱动器按入口补齐契约（`--feed-event-info`、`CAMPUS_E2E_FEED_ORIGIN`、服务端
`CAMPUS_TRUSTED_GATEWAY_ORIGINS`；`--manifest`、`CAMPUS_E2E_FIXTURE`、不存在的数据目录），
并让每条入口的证据落进发货布局 `verification/browser/<entry>/artifacts/`。两条入口复跑即过：
feed-mode 6 项（含此前跳过的供货证据检查）、reliability 6 项。

## 三、把契约钉住，避免第三次漏

`entry_launch_plan()` 提取为纯函数（不碰磁盘、可注入 base_env），
`server/campus/tests/test_ai_round_browser_plan.py` 6 例 / 16 subtests 逐条断言：
每条入口的端口与输出布局、feed-mode 的供稿种子与信任来源、reliability 的模块/manifest/
新数据目录、普通入口**不得**携带任何入口专属变量、OCR 是显式 opt-in、base_env 只拷贝不改。
反向验证：删掉信任来源那一行，恰好且仅有 1 例失败（`test_feed_mode_...trusts_the_fixture_origin`）。

**踩坑记录（对后续维护者重要）**：验证完契约后用 `git checkout -- tools/ai_round.py`
清理"破坏性实验"的改动，把**全部未提交的驱动器修改**整文件回滚了（该文件自 0503dad 后
一直干净在索引里）。重建依靠的是本轮完整的编辑记录，没有数据丢失，但教训明确：
**未提交文件的恢复永远不用 checkout 类命令**，先备份再实验。

## 四、最终全量（提交前最后一次，用最终版驱动器）

`ai_round.py --browser --ocr`：**12/13**——9 个浏览器入口全过（`browser` 11 项含真实
中文 OCR、`a11y` 17 审计 0 违规、`ui-polish` 20、`ui-review` 7、`feed-mode` 6、
`source-live` 8、`source-selection` 8、`highlight-contrast` 1、`reliability` 6，
与 1.9 轮的 84 项一致），后端 **622 passed / 8 skipped / 0 failed / 122 subtests**
（第 3 轮新增 6 例），前端 33 passed，前置 hygiene 通过。唯一 FAIL 是 release gate：
后端 suite 在轮次内重写 `backend.xml` 与运行日志的机器路径——这是既定流程，
规范化后 `--check` 为 0（`0 file(s), 0 path(s) would be rewritten`）。

## 五、收尾清洁

- 删除根目录 4 个会随清单发货的散落输出目录（驱动器现在写发货布局）。
- 删除空的 `data/campus.sqlite3-wal`（0 字节）与 `-shm`：主库 SHA-256 与清单记录
  **逐字节一致**，未动任何数据；清单构建器拒绝发货 WAL 边车的行为本身被再次验证。
- `RELEASE_MANIFEST.json` 重建：**763 项**（758 + 第 2 轮 3 个工程文件 + 本轮 2 个：验收契约测试与 SELF_EVALUATION.md）。
- 每小时自动化（T8）已存在但为 PAUSED；是否启用交给维护者决定。

## 第 3.5 轮 —— 自评后再提升：把"既定流程"修成缺陷（2026-10-13）

SELF_EVALUATION.md 定稿后，按用户指令"评价完再提升"再循环一次：拿自评里连续写了三轮的
一条记录开刀。那句话是："唯一非绿项 release gate 是既定流程——后端 suite 在轮次内重写
自己的证据，规范化后为 0。"按自评第七节自定的标准 2（一条命令复现且全绿），这句话本身就是
没达标的证据，于是按缺陷处理，不当文档成本。

**复现（先跑再修）**：`ai_round.py` 无参一轮 → 4/5，release gate 报
"2 file(s), 11 path(s) would be rewritten"。连续三轮如此，每轮都要人工补一条规范化命令。

**修复**（驱动器，产品代码零改动）：`check_backend`/浏览器入口写证据之后、release gate
之前插入 `normalize evidence` 步骤——`normalize_evidence_paths.py --root .`（不带
`--check`）。pre-flight 不变，仍先跑、仍能抓住"轮次前就脏"的树；新步骤只碰本轮产生的证据，
且作为独立步骤报告。原 docstring"刻意不在背后重写证据"的取舍前提（防掩盖既有泄漏）已由
pre-flight 覆盖，代价（每轮一条人工命令 + 一条 FAIL）却要每轮支付——前提变了，结论就该变。

**钉住**：`RoundFlowTests` 解析驱动器源码 AST，断言 `check_backend` →
`normalize_evidence` → release gate 的顺序，且规范化调用带 `--root`、不带 `--check`。
反向验证：从 `main()` 删掉规范化调用 → 恰好 2 例失败（另 6 例不受影响）。

**结果**：稳态一轮 **5/5**（pre-flight / backend / frontend / normalize / release gate）。
复跑第一轮 pre-flight 失败是正确行为——它如实报告上一轮未规范化的遗留，证明没被绕过。
后端 622 → **624**（新增 2 例流程测试）；`ruff check` / `format --check` 全绿。

**未验证**：与第 3 轮相同清单（PostgreSQL 8 例、镜像、真实采集/SMTP、OCR 需联网下载模型、
每小时自动化为 PAUSED），无新增未验证项。

## 第 4 轮 —— 打包成交付 ZIP 并三重验证（2026-10-13）

自评结论里唯一"比交付物差"的一点是没有打包件。本轮补上，且验证不复用打包工具自己的结论：

- **打包**：按清单暂存 763 个文件到 `偷闲天大版-2.0-品位版-自主维护第2轮/`，
  `tools/package_release.py` 生成 `偷闲天大版-2.0-品位版-自主维护第2轮.zip`
  （764 项 = 763 + 清单自身，21 MB）+ `.sha256` 边车（验证通过后才写边车）。
- **三重验证**：① 打包工具内建 verify（763 files verified）；② 按文档命令
  `build_release_manifest.py --root <项目树> --verify <zip>` 独立复验 exit 0；
  ③ `sha256sum -c` 核对边车 OK。包内清单与项目树清单逐字节一致。
- **可替换性对比**（对桌面交付包逐文件比 SHA-256）：**0 丢失**、新增 5 个
  （ruff.toml、CI 工作流、pre-commit 配置、SELF_EVALUATION.md、浏览器契约测试）、
  内容变化 159 个——其中 verification 79 个（本轮重跑的 evidence）、server 63 个
  （绝大部分是第 2 轮 lint/format 机械归一化）、docs 5 个、tools 5 个；
  **data/、dist/、web/ 零变化**（样例库 SHA-256 未变，前端未重建）。
- **已知边界**（如实）：归档是内容验证而非字节可复现（条目时间戳来自文件 mtime）；
  包内不含 Playwright/Chromium 与 OCR 模型，浏览器验收需使用者本机自备；
  CI 工作流未经真实 GitHub 环境执行。暂存目录验证后已删除。

## 第 5 轮 —— 真实上线：单端口托管启动器与公网实例（2026-10-13）

自评结论里"未部署公网服务"一项，本轮真正做掉：不是演示截图，是一个公网 HTTPS 实例。

- **启动器**：根目录新增 `serve_cloud.py`（单端口托管沙盒专用）。读取平台注入 `PORT` 绑定 `0.0.0.0`；启动前校验数据目录可写、SQLite 完整、前端构建（与 `app.py` 同一套 bootstrap 函数）；关闭公开 API 文档。两种模式只差启动期环境默认值，产品代码零改动：
  - `demo`（默认）：空库一次性写入**【演示】**合成事项；显式打开开发验证码（无 SMTP 的实例因此可登录，验证码界面直接显示）；`demo-admin@touxian.local` 为管理员；worker 关闭。
  - `live`：与 `app.py` 相同的公开守卫——**即使环境继承了开发验证码变量也强制关闭**；一次性注册 `config/campus-sources.json` 的 15 个官方来源；worker 随之采集。
- **真问题与真修复**：宿主域名在启动时未知，产品按固定 `CAMPUS_ALLOWED_ORIGINS` 校验写请求来源，公网实例的一切登录都会被拒。启动器放行"来源恰为当前宿主"的写请求（同源语义），跨站伪造来源仍被 `ORIGIN_FORBIDDEN` 拒绝——用 4 例测试钉住两个方向，先红后绿。
- **公网核验**（对实例逐项实测，非本地复述）：健康 200；前端页面与 `/assets/` 200；3 条演示事项列表与详情 200；检索返回命中与摘录；登录换会话后 `/me/feed` 200；管理员 `/admin/jobs` 200；`/openapi.json` 404；伪造跨站来源 403。上线链接记录在 `README-ONLINE.md`。
- **如实记录的失败**：`live` 模式先在同一沙盒真实部署验证过——15 个来源注册成功但健康检查全部失败、采集 0 条：托管沙盒出网只能到平台自身端点与 pip 镜像，抓不到 `*.tju.edu.cn`。因此公网实例以 `demo` 模式运行；真实采集需在可达校园网的环境用 `CAMPUS_CLOUD_MODE=live` 部署。沙盒持久性由托管方决定，未核验。
- **测试**：`server/campus/tests/test_cloud_launcher.py` 13 例（模式默认值、live 强制守卫、显式环境优先、公开 origin 合并、PORT/0.0.0.0、空库才播种/注册、同源匹配、demo 启动冒烟）。全套后端 **637 passed / 8 skipped / 0 failed / 122 subtests**。

## 第 6 轮 —— 真实接入官网与公众号：核验到什么程度、卡在哪（2026-10-13）

目标改为「真实接入」。本轮把三件事查实并落成证据，没有为了好看放宽任何安全边界：

- **官网真实可达（服务端核验）**：`oaa.tju.edu.cn/bszy/tzgg.htm` 返回真实列表（20 条，最新
  2026-09-14 四六级报名）；文章页 `info/1059/9030.htm` 服务端渲染完整（标题、发布时间、正文、
  截止「2026-04-15 前提交申请材料」、材料与附件指向）。**重要事实**：真实列表混合官网文章与
  `mp.weixin.qq.com/s/...` 公众号文章两类链接——官网接入与公众号文章接入是同一条采集链路。
- **公众号文章真实可解析（服务端核验）**：`mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw`
  （账号「天津大学教务处」）服务端渲染完整正文，无验证码、无登录墙。`content.py` 的 `#js_content`
  提取与 JS 揭示处理覆盖该形态；3 个教务处来源的 `article_url_pattern` 已接受 `mp.weixin.qq.com/s`。
- **本环境两处都不可达（实测，非推断）**：开发机 `oaa.tju.edu.cn` 解析到 `198.18.0.133`
  （fake-IP，非全局地址），直连与本机代理均失败（curl 000），产品 SafeFetcher 正确报
  `unsafe_address`；托管沙盒 live 模式 15 个来源健康检查全部失败、采集 0 条。**没有**为此放宽
  DNS/SSRF 检查（任务池 N4 的既有结论，再次实测成立）。
- **公共 feed 不可用（实测）**：`rsshub.app/wechat/oufenchang` 403。公众号合规接入因此限定为
  运营方自建 WeRSS / RSSHub / 授权接口三条路（`WECHAT_FEED_GUIDE.md`）。
- **产出**：`REAL_INGESTION.md` 运行手册（前提自检 → probe → live 采集 → 四级验收 → 公众号三路径
  → 上线 → 明确不做清单）；`verification/source-reachability-2026-10-13.json` 证据；
  README-ONLINE.md 增加真实接入现状小节。产品代码与配置**零改动**。
- **未验证**：真实采集一轮的端到端结果（需可达主机）；任何公众号授权 feed（需运营方扫码或端点）。
  这两项只有运营方能提供，无法由本环境代替。

## 第 7 轮 —— 用真实页面原文做红测：修复「于〈日期〉前」截止缺口（2026-10-13）

第 6 轮拿到两篇真实文章原文后，本轮把原文里的截止句逐条喂给抽取器——真实采集上线后会漏的错，
必须在本地先犯一次：

- **发现（3 句真实原文全部漏抽）**：「请考生所在学院**于2026年12月15日前**统一报送解除限报材料」、
  「**最晚于26年9月18日16:30前**上报学籍信息」、「**应于2026年9月24日（报名结束前）**提出合理便利申请」。
  日期跨度本身都找到了（连两位数年份「26年」都正确锚定），但全部死在 `DATE_CUE` 门控：
  真实通知里「请」和「于」之间隔着「考生所在学院」，「前」后面也不是「提交」而是「统一报送/上报」。
  这类「（最晚/应/须）于〈具体日期〉…前」是向学院/学生布置的**真实办理截止**，漏掉它等于漏掉事项。
- **修复**：新增 `BEFORE_DATE_CUE`（`于[^，。；]{0,12}\d+[日号][^，。；]{0,8}前`），同时并入
  `DATE_CUE` 与 `DEADLINE_CUE`；`EXTRACTOR_VERSION` 1.5 → **1.6**（用户可见的抽取规则变化）。
- **先红后绿**：`server/campus/tests/test_real_page_deadlines.py` 4 例（三句原文 + 一条无反例：
  「于校内各网点办理」不含日期不得产出）。修复前 3 红 1 绿；修复后 4 绿。反向对照：抽取审计
  （`test_extraction_audit`）、正文质量（`test_body_quality`）与合成反例集（`test_extraction_accuracy`，
  96 项）在修复后**全绿**——随包样本行为未变，新 cue 没有把任何叙述句误判成截止。
- **实测输出**：三句分别得到 `college_deadline 2026-12-15`（含「学院」→ 院级截止，产品原有区分）、
  `school_deadline minute at=2026-09-18T16:30+08:00`（分钟精度落 `at` 字段）、
  `school_deadline 2026-09-24`（括号插在日期与「前」之间也成立）。
- **全套**：**641 passed / 8 skipped / 0 failed / 122 subtests**（较第 5 轮 +4，全部来自真实页面用例）。
- 产品其余部分零改动；本轮的三个新文件/改动都在抽取 cue 与其测试内。

## 第 8 轮 —— 运营方域名清单逐条实测，来源目录 15 → 19（2026-10-13）

运营方在对话中给出 11 个官网首页与 16 个 `mp.weixin.qq.com/s` 文章链接，要求真实接入。
本机与托管沙盒都抓不到这些域，因此逐条做**服务端抓取**判读（不是产品采集器的健康检查，
两者在 `REAL_INGESTION.md` 里有明确区分）：

- **官网 11 条**：oaa / gs / job / scet 在册且复测有效；**新增 4 个来源**——本科招生网
  通知公告（`zs.tju.edu.cn/ym21/bkzn/tzgg.htm`，文章形如 `/info/1116/4829.htm`）、迎新网
  本科迎新快讯（`yx.tju.edu.cn/`，站点无独立列表页，首页「迎新快讯」即列表，文章形如
  `/yxkx/bks/202608/t20260815_324689.htm`）、教务处通知公告总览（`/tzgg.htm`，混合官网与
  公众号两类链接）、教务处竞赛科创（`/tzgg/js_kc.htm`，含四六级报名通知）。
  `www.tju.edu.cn`（新闻网聚合，无通知列表）、`lib.tju.edu.cn`（JS 单页）、`www.twt.edu.cn`
  （学生工作室站）**不构成可用列表，未注册**；`i.tju.edu.cn`、`zzzx.tju.edu.cn` 两次抓取
  均失败，域名用途需运营方确认，**未注册**。
- **公众号 16 条**：逐条抓取，微信公众平台统一返回「参数错误」（4 条首次传输层失败、重试
  同样参数错误）——没有一条对应真实文章。真实文章可解析性另由 2026-10-13 的教务处四六级
  报名文章（`/s/CSniYqr1LfSYobIeCZA6nw`）证明；采集入口是列表页，单篇文章链接本就不作为
  来源注册。
- **实现**：`config/campus-sources.json` +4 条目（id 为 `uuid5(NAMESPACE_URL, 'tju-campus-source:' + origin_url)`），
  `sources.py` builtins 同步登记（seed 只认两侧一致的来源，配置不能自封官方身份）；
  `test_source_catalog.py` 新增第 8 轮专测（混合链接只收官网与公众号原文、拒相似主机；
  迎新 URL 形状单独分支）；`test_network_probe.py` 计数钉住 15 → 19——测试如实失败后更新，
  不是改产品迁就测试。
- **证据**：`verification/operator-sources-2026-10-13.json`（27 条 URL 逐条结论 + 未验证项）；
  `SOURCE_COVERAGE.md`、`README-ONLINE.md`、`REAL_INGESTION.md`（新增第 4 节实测结论表）同步。
- **全套**：**642 passed / 8 skipped / 0 failed / 130 subtests**（subtests 122 → 130：新增 4 个来源
  各进入通用列表形状用例；JUnit `tests=780`）。
- **未验证**：19 个来源的产品侧健康检查仍须在可达主机按四级验收执行；i/zzzx 域名用途待确认。

## 第 9 轮 —— 首个真实运营周期：中继把官网原文送进产品全链路（2026-10-13）

运营方要求「你运营」。本机与托管沙盒都抓不到 `*.tju.edu.cn`（N4：产品拒绝保留地址是
正确行为），因此本轮建成并跑通**运营中继**架构：运营方是唯一能触达官网的组件，
中继把官方页面**原样**送进产品自己的授权 feed 管道；feed 条目 URL 永远是真实官网
地址（`feed_archive.original_url` 强制 https + 公网主机），候选、证据链接、发布
事件全部指向原始公告——中继是传输，不是身份。

- **工具链**（均为新增，产品采集/安全代码零改动）：`deploy/operator/relay.py`
  （stage→build→serve→register→report→decide 六个子命令）、`deploy/operator/OPERATIONS.md`
  （操作规程 + 四级验收阶梯 + 踩坑记录）、`serve_cloud.py` 新增 `operator` 模式
  （worker 内置、dev-auth 开、**绝不播种合成数据**、默认只绑 127.0.0.1）；
  `CAMPUS_TRUSTED_GATEWAY_ORIGINS=http://127.0.0.1:8791` 精确放行一个回环源。
- **周期 1 实测**：2 个中继源注册即 `healthy/verified_official`，4 篇真实原文
  （招生章程、强基计划校考安排、四六级报名通知、AI+合成生物大赛）采集→解析→抽取
  全成功；**3 条事件发布**（3 accept），强基计划候选因抽取不完整被 **reject**
  （发布无日期的校考安排会误导考生），发布门与审核闸都按设计工作。
  `GET /api/v1/events` 3 条 published；`/api/v1/search` 召回全部 3 条
  （index_status=current，本地 FTS5）；事件的 evidence 链接是真实官网/公众号原文。
- **第 7 轮修复在真实原文上复现**：四六级通知产出
  `college_deadline 2026-12-15`（学院限报解除）、`school_deadline` 分钟精度
  `at=2026-09-24T17:00:00+08:00`（报名窗口 9月22日7:00—9月24日17:00 的结束时刻）、
  `school_deadline 2026-09-24`（合理便利申请），另有 2 行 `precision=unknown`
  如实标注「报名截止后不再补报」这类无日期陈述。
- **完整性链**（可机器复核）：staging 文本→feed 逐字节可重建；每篇快照
  `sha256(blob)==content_hash`；中继原样正文完整出现在产品归档文档内
  （归档 = 产品固有外壳 h1+published_time + 中继正文，不改写）；
  `capture.method=authorized_feed, feed_origin=http://127.0.0.1:8791`。
- **过程中修的产品缺陷**（均先红后绿）：
  ① 写端点幂等——所有 POST 必须带 `Idempotency-Key`（缺了 422 `idempotency_required`），
  中继按调用点传稳定键（create 按身份、verification 按源按天、fetch 按周期）；
  ② `AdminSession` 持久化会话 cookie——dev-auth 每邮箱每小时 6 个验证码且 30 秒不重发，
  逐子命令登录会把运营方锁在门外（本轮实测被锁 50 分钟后修复，4 例单测含变异验证）；
  ③ `report/decide` 用错候选形状（`id` vs `candidate_id`），修正并补分页；
  ④ **发布卫生**：清单构建器不读 `.gitignore`，`deploy/operator/session.json`（活 cookie）、
  `data-operator/`（运行实例库）、`deploy/operator/out/`（可再生 feed）会被哈希进出货物——
  改为拒绝/排除，`test_release_manifest_guards.py` 4 例钉住。
- **新任务 T19**：日程型日期抽取缺口——「校考于6月30日…举行」「报到时间为6月30日13:00-13:30」
  「可于6月28日12:00起登录」一条不产（`EVENT_TIME_CUE` 只认「考试/活动/…时间」与行首
  「时间：」，`DEADLINE_CUE` 无日程句式）。已用真实原文与 cue 模式实测定位，修复前先写红测。
- **全套**：**657 passed / 8 skipped / 0 failed / 130 subtests**（200.22s，exit 0；
  JUnit `tests=795`）。642 → 657：+9 中继测试、+2 operator 启动器、+4 清单守卫。
  第 8 轮证据运行早于中继工具链，本轮补齐其覆盖。
- **证据**：`verification/operator-cycle-1-2026-10-13.json`（注册/完整性链/候选/决策/
  发布/检索/未验证项）、`deploy/operator/registration.json`、`cycle-report.json`。
- **未验证**：截止时间正确性为运营方人工读原文结论；operator 模式未启用 MeiliSearch；
  强基计划待 T19 后随下周期重发；i/zzzx 域名用途与 16 个失效公众号链接仍待运营方输入。

## 第 10 轮 —— 交接打包：AI 运营者入口与两份移交物（2026-10-15）

运营方要求「打包好让其他 AI 操作」。本轮不动产品一行代码，做三件事：
把接手路径写成 AI 能直接执行的入口文档、把真实数据快照公开发布、
把全部材料（代码+文档+ git 历史）打成可移交的包。

- **`AGENTS.md`（新增，仓库根）**：AI 运营者唯一入口。写清：这是什么（含契约冻结
  v1.5.0）、上手 10 分钟路径、质量门（红测先行/出厂前全套回归/诚实记录/详细提交/
  重建清单再打包）、三种运行模式、真实采集两条通路（校园网直采 / 运营方中继——
  feed 是传输不是身份）、7 条天坑（Windows basetemp、CRLF、写端点双头、验证码限额、
  评审队列 candidate_id、搜索键 hits、中文侧链 UTF-8）、当前基线、边界（什么能碰
  什么不能碰）。详细深度仍指向 AI_PROJECT_CONTEXT / AI_PENDING_TASKS / OPERATIONS.md，
  不重复抄。
- **真实数据公网快照上线**：`data-operator/` 的一致性快照（SQLite backup + WAL
  checkpoint）打进第 5 轮发布包原样树，live 模式 + 关 worker 重发：
  `https://ec728a4b88f943a7b3f2bc39b7d22d52.sg2.agentos-app.run`。逐项核验：
  首页 200、3 条周期 1 真实事项、evidence 接口返回真实官网地址、搜索可召回、
  **登录接口 503 且不再泄露开发登录码**（公网 challenge 实测无 `X-Dev-Login-Code` 头）。
  发布前从活库删掉 7 个集合（会话/登录码/账号/订阅/摘要/幂等/画像）——
  否则等于把能登进的 cookie 一起发到公网。核验记录写入 `VERIFICATION.json`
  的 `public_real_snapshot` 块。
- **本轮实测到的新坑（已写进 CONTEXT 第三节）**：`--basetemp` 的**父目录**不存在时，
  pytest 建 basetemp 报 WinError 3，151 个依赖 `tmp_path` 的用例全部 setup 假错
  （506 passed 之外全错）——先 `mkdir -p` 父目录再跑即恢复 657 全绿。
  第 9 轮没碰上是因为当时父目录恰好已存在。
- **全套在出厂树上重跑**：**657 passed / 8 skipped / 0 failed / 130 subtests**
  （162.55s，exit 0；JUnit `tests=795`，failures/errors=0）。数字与第 9 轮一致，
  证实本轮是纯文档 diff。
- **清单与打包**：`RELEASE_MANIFEST.json` 重建为 **789** 项（+1 AGENTS.md）；
  发布包重打为「偷闲天大版-2.0-品位版-自主维护第 6 轮」并重新三方核验；
  git 历史（前 10 轮全部提交）打成 `campus-improved-history.bundle`，
  下一个 AI 可 `git clone` 直接拿到完整语境。
- **文档同步**：CONTEXT 目录速查加 AGENTS.md 行、基线段更新（789/第 10 轮/两个公网实例）、
  第 45 行那条与坑①③矛盾的旧注释（「默认 basetemp 即可」）修正为必须 `--basetemp`；
  交付说明改称第 6 轮并补交接节；VERIFICATION.json 记录本轮重跑与快照核验。
- **未验证/待办（截至本轮结束）**：T19（赛程日期无 cue）本轮未修——**第 11 轮随即修复**；
  旧 demo 实例仍暴露开发登录码（合成数据，待择机按 live 重发）；
  i/zzzx 域名用途与 16 个失效公众号链接仍待运营方输入。

## 第 11 轮 —— 修 T19：日程型日期不再一条不产，且不许升格成截止（2026-10-15）

周期 1 的强基计划校考通知抽不出任何日期：学生看到「校考安排」，却看不到校考几点、
报到几点、准考证什么时候能打。本轮把它补上，并把「日程不是截止」这条边界钉死。

- **红测先行**：`server/campus/tests/test_real_page_schedule.py` 5 例，全部取自
  `deploy/operator/staging/zs-notices/强基计划校考工作安排.txt` 的真实原文——
  「校考于6月30日在天津大学卫津路校区（…）举行」「报到时间为6月30日13:00-13:30」
  「可于6月28日12:00起登录」三句，加两条边界：日程句不得被判成 deadline，以及沿用
  第 7 轮的反例「于校内各网点办理」仍不得产出。修复前 **4 红 1 绿**。
- **定位**：三句都死在 `date_values()` 的第一道门
  `if not re.search(DATE_CUE, text): return []` —— 不是分类错，是根本没进抽取。
  数字日期本身解析正常，缺的是「日程」这一类措辞。
- **修复**：新增 `SCHEDULE_CUE`，覆盖三种真实句式——`〈日程名词〉时间`（报到时间、
  校考时间…）、`于〈日期〉…起`、`〈日程名词〉…于〈日期〉`。它**并入 `DATE_CUE`（开门）
  与 `EVENT_TIME_CUE`（定性 event_time），刻意不并入 `DEADLINE_CUE`**：日程句里没有
  「截止/前」语义，把它标成截止等于告诉学生校考必须在当天完成。
  `EXTRACTOR_VERSION` 1.6 → **1.7**（用户可见的抽取规则变化）。
- **实测落点**：`event_time` 日期精度 `2026-06-30`；`event_time` 分钟精度
  `at=2026-06-30T13:00:00+08:00`（报到窗口起点）；`event_time` 分钟精度
  `at=2026-06-28T12:00:00+08:00`（准考证打印窗口开启）。三句 `DEADLINE_CUE` 实测均不命中。
- **反向对照**：抽取审计（`test_extraction_audit`）、正文质量（`test_body_quality`）、
  96 项合成反例（`test_extraction_accuracy`）与第 7 轮真实截止用例
  （`test_real_page_deadlines`）修复后**全绿**，随包样本一行未变。唯一需要改的既有用例是
  `test_extractor_version_documents_the_current_rules` —— 它本来就钉版本号，改规则就得改它，
  这正是它的用途。
- **顺手修三处出厂卫生**（都不是产品缺陷，是上一轮尚未提交的工作留下的，或本轮全跑暴露的）：
  ① 上一轮把 `verification/audit/backend-suite.log` 重新生成成带绝对解释器路径的版本，
  却没有再跑规范化，全套因此 **2 failed**（`test_evidence_tree_hygiene` 的两条）。按用例
  自己的提示运行 `python tools/normalize_evidence_paths.py --root .` 后复绿；该工具幂等
  （复跑报 0 改写）。
  ② 上一轮在 `.snapshot-tmp/` 里干活，留下 `update_verification.py` 没清；这个名字**既不在
  `.gitignore` 也不在清单构建器的排除名单里**，于是重建清单时这个临时脚本被当成交付文件
  哈希进去（789 → 791，多出来的第二项就是它）。修法：`.gitignore` 与
  `IGNORED_DIR_PREFIXES` 各加 `.snapshot-tmp`，删掉残留目录，并新增守卫用例
  `test_an_ad_hoc_scratch_directory_does_not_ship`，钉住「临时目录不得悄悄撑大清单」。
  清单恢复为 **790**（789 + 本轮新测试文件）。
  ③ **`test_bootstrap` 的 Windows teardown 竞态**：本轮全跑时
  `test_cli_starts_on_loopback_with_isolated_data_and_worker_disabled` 偶发失败——
  子进程被 `taskkill /T /F` 强杀、`process.wait()` 已返回之后，Windows 仍在异步释放
  文件句柄，`tearDown` 里的 `TemporaryDirectory.cleanup()` 撞 `WinError 32`（文件被占用）。
  这是真实存在的平台竞态（不是沙盒专属），会给接手的人白送一次假失败。
  修法：`tearDown` 对清理做**有界重试**（20 × 0.1s，最后一次仍失败就抛），
  既不掩盖真正的持久占用，也不让竞态污染无关的用例。
- **全套在出厂树上重跑**：**663 passed / 8 skipped / 0 failed / 130 subtests**
  （146.63s，exit 0；JUnit `tests=801`，failures/errors=0）。657 → 663：+5 来自 `test_real_page_schedule.py`，+1 来自新增的清单
  守卫用例。前端单元 **33 passed / 0 failed**。此后仅改文档（本文件、`AI_PENDING_TASKS.md`、
  `AI_PROJECT_CONTEXT.md`、`AGENTS.md`），未再动代码，沿用本轮套件结果。
- **未验证**：三句落点是对**运营方暂存文本**的实测，不是对官网实时页面的核验
  （本机与托管沙盒都到不了 `*.tju.edu.cn`，见 CONTEXT「已知未验证」）；强基计划候选
  仍待运营方下一周期重新采集后复核发布。

## 第 12 轮 —— 修 T6：搜索命中携带 `ended`，与信息流「已截止/取消」口径一致（2026-10-16）

输入：运营方将 `AI_PROJECT_CONTEXT.md` 与 `AI_IMPROVEMENT_LOG.md`（第 11 轮版）复制到桌面并上传，
未附指令。本轮按 AGENTS.md 接手路径自主执行：定位出厂树（`WorkBuddy AI/2026-10-12-18-11-35/output/campus-improved`，
HEAD `ed3aab7`）、恢复工作区被删的两份记忆文档（`git checkout --`）、做验证补齐 + 任务池改进。

### 一、基线刷新（改动前，出厂树全量重跑）

| 项 | 结果 |
|---|---|
| 后端全量 | **663 passed / 8 skipped / 0 failed / 130 subtests**（119s） |
| 前端单元 | **33 passed / 0 failed** |
| 浏览器验收 | **9 入口全过**：browser 11 项（含 OCR 中文识别）、a11y 12 页 0 违规、ui-polish 12 项、ui-review 7 项、highlight-contrast 2 mark 6.58:1、source-live 8 项、source-selection 8 项、feed-mode 6 项、reliability 8 项 |
| 发布门禁 | path hygiene 0/0 |

第 11 轮树完整可用；**浏览器验收证据刷新为本轮**（此前停留在第 4 轮）。

### 二、T6 缺口与证据

信息流页（Today）与机会页（PublicEvents）已用 `feedView.eventHasEnded()` 把已截止/取消折叠为
「历史通知」；但搜索页 `SearchHit` 载荷（`event_id/event_version/title/excerpt/source_url/evidence_id/historical`）
**没有截止信息**，同一已过期事项在搜索结果里看不出已截止，与信息流口径不一致。
真实数据验证（data/ 只读副本）：52 个搜索命中中 **14 个**已过全部截止时间
（如「天津大学智能与计算学部2026年博士研究生招生办法」「2026年伦敦大学学院暑期学分项目报名通知」），
若不标识，学生会把过期事项当作当前有效事项。

### 三、方案（B 级，动冻结契约 1.5.0，向后兼容的字段新增）

`SearchHit` 增加 required `ended: boolean`，服务端按与前端 `eventHasEnded()` **完全一致**的判定计算：
`cancelled → true`；deadlines 排除 `event_time` 取 closing（无 closing 则全量）；relevant 非空且
**全部** passed → true；date 精度用 Asia/Shanghai 今日字符串比较、minute 精度用绝对时刻比较；
缺失/unknown 一律不算 passed（与信息流「宁可让学生自己确认」一致）。

改动文件：
- `campus-contracts/domain-v1.schema.json` / `openapi.v1.json`：SearchHit + `ended`（required）。
- `server/campus/search.py`：新增模块级纯函数 `_deadline_passed` / `_event_ended`（复刻前端语义，
  tzdata 已在 lock 内，`ZoneInfo("Asia/Shanghai")` 可用）；`hits()` 注入
  `ended=_event_ended(event, now)`，`now` 取自 `core.delivery.clock()`（测试可固定时间）。
- `src/campus/App.jsx`：搜索页 card-top 渲染 `{hit.ended && <span className="tag warning">已截止</span>}`。
- `server/campus/tests/test_search_ended.py`（新增，16 例纯函数）+ `test_integration.py`（1 例集成：
  发布「已截止/进行中」两事件，搜索断言 `ended` 为 True/False；`prepare_notice` 增加 `title` 参数，默认值不变）。
- `dist/` + `web/` 重建：`npm ci`（79 包）+ `vite build`（vite 6.4.4），两目录逐字节一致（10 文件）。

### 四、红测先行与两处测试侧踩坑

先改契约（schema + openapi）→ `test_integration` 2 例红（422 VALIDATION_ERROR 缺 `ended`）；
实现后转绿。新测试首跑撞两坑，均已修复并记录：
1. **验证码 30 秒冷却**：两个事件各登录 admin 一次 → 第二次 429 RATE_LIMITED；
   复用同一 `admin` 会话解决。
2. **测试标题含「截止」被抽成 unknown 截止**：标题「合成集成测试已截止事项」含「截止」字样，
   被抽取器当截止陈述，产出 `precision=unknown` 的 noise deadline，`ended` 判为 False。
   这是**测试数据问题**（改标题规避），但揭示了真实噪声方向：**标题含「截止」会被抽成 unknown 截止**
   （与 T5 处理的小节标题同类）——记入任务池 **T20**（P3）。

### 五、全套结果

- 后端全量（改动后，干净环境重跑）：**680 passed / 8 skipped / 0 failed / 130 subtests**
  （111s；+17 例全部来自本轮新测试）。
- 前端单元 **33 passed / 0 failed**；浏览器 9 入口**全过**（截图与报告已刷新）。
- **hygiene 门禁一次失败的自救记录**（如实写明，避免接手者再踩）：首轮全量回归 exit 1，
  失败不在产品代码——后端/前端/浏览器全绿，是 2 个 hygiene 用例红：
  ① 临时验证文件（`check_ended_realdata.py/.log`）含机器路径——已删除；
  ② `verification/audit/backend-suite.log` 记录了 hygiene 失败时的断言输出，
  其中引用了已删文件的名字与路径片段，形成**自引用污染**（normalize 只处理 Windows 盘符路径，
  不处理断言消息里的 POSIX 家目录（用户目录）路径片段）——干净环境重跑后端套件重新生成该日志后，
  `normalize_evidence_paths.py --root .` 幂等复绿（复跑 0/0），hygiene 5 passed。
- 真实数据验证：data/ 只读副本搜索 4 个关键词，52 命中中 14 例 `ended=True`，契约形状校验通过。
- 清单重建：790 → **792** 项（+1 `test_search_ended.py`；dist/web 与证据哈希更新）。

### 六、实际改善与发布边界

- 搜索页不再把已过期事项当有效事项展示；搜索口径与信息流折叠口径一致（同一 `ended` 判定）。
- **未发布声明**：本轮未重发任何公网实例（公网实例仍是第 10 轮快照；新契约只在本地树验证，
  dist/web 已重建待发）。

### 七、未验证 / 待办（如实）

- 前端「已截止」标签**未做浏览器级断言**（demo 种子无已截止事件；集成测试 + 构建验证了语义与
  渲染代码，未做像素级确认）。
- T6 的另一半（「20 个已发布事件截止时间已过仍 published」）按产品设计保留：事件身份不自动撤回、
  不删除，由前端折叠，**非缺陷**。
- T20（新）：标题含「截止」被抽成 unknown 截止——P3，与 T5 同类，建议并入后续抽取质量轮。
- 强基计划候选、i/zzzx 域名用途、16 个失效公众号链接仍待运营方输入。

### 八、下轮建议

- T10（近义词命中高亮）是下一个同类 B 级项（SearchHit 加命中词字段），实施路径与 T6 相同；
- 或等运营方提供强基计划采集材料后复核发布；验证码 30 秒冷却已记录，测试设计避免连续同邮箱登录。


## 第 13 轮 —— 公网更新部署准备：第 12 轮发布包 + 干净数据快照（2026-10-16）

输入：运营方在第 12 轮交付后说「让它可以真正使用」。按澄清确认：目标 = 更新公网实例
（最新代码+真实数据重发），且运营方有平台环境/可达主机可用。本轮职责 = 备好部署弹药
（发布包 + 干净快照 + 部署手册），**发布动作本身由运营方在有通道的环境执行**。

- **公网现状核验（对实例实测）**：真实快照实例 `ec728a4b…` **仍在线**——health 200
  （`demo_data:false`）、首页 200、events 接口返回周期 1 真实事件，但跑的是第 10 轮旧代码
  （无第 12 轮「已截止」标识）；demo 实例 `6923f20c…` 本轮核验**返回 400**（疑似下线/回收）。
- **部署通道事实**：本机无 agentos 平台部署 CLI/接口/凭证（历史公网实例均在托管沙盒平台侧
  发布），因此本轮不做「假装发布」，只产出可交付的部署物并如实标注边界。
- **干净数据快照**（`deploy/snapshot/campus-snapshot-2026-10-16.sqlite3`，1,175,552 B，
  sha256 `ae0a36a0…97ba4`）：对 `data-operator/` 活库做一致性备份（sqlite backup，含 WAL），
  在**备份副本**上擦除 7 个隐私集合共 **35 行**（sessions 6 / challenges 12 / users 2 /
  profiles 2 / subscriptions 2 / digests 2 / idempotency 9）——这些含会话 CSRF、登录码挑战、
  邮箱、画像、订阅、个人摘要与限流键，**不得进任何公开部署**；`integrity_check=ok`，
  保留 events 3 条（周期 1 已发布）、candidates 4（3 采纳 1 拒绝）、reviews 4、sources 21、
  evidence 85、搜索索引 142 等共 224 条。**活库未被改动**。
- **发布包**：清单 792 → **794**（+快照 +README）；`package_release.py` 打包
  「偷闲天大版-2.0-品位版-第12轮发布包.zip」（795 条目含清单自身，sha256
  `82643229…7c3978`，21.5 MB），包内核验 **CLEAN**（无 data-operator/、无 session.json、
  无 .server-secret、无 -wal/-shm、无 deploy/operator/out），快照哈希与包内一致。
- **部署手册**（`deploy/snapshot/DEPLOY.md`）：解包→放快照→live 模式+关 worker 启动的
  精确环境变量，外加 **9 项部署后核验清单**（health / 首页与前端 / events 3 条真实 /
  search 命中含 `ended` 字段 / auth 无 `X-Dev-Login-Code` / 跨站伪造 403 / openapi 404 /
  数据目录无 sidecar 泄漏）。
- **未验证 / 待办（如实）**：真正部署由运营方执行后回填新 URL 与 9 项核验结果；快照数据
  截止 2026-10-13，强基计划候选待运营方下周期重新采集复核后并入下一份快照；demo 实例 400
  的原因（平台回收/过期）未向托管方确认；沙盒文件系统持久性仍由托管方决定未核验。
