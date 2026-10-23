# 第 3 轮验收：浏览器验收拉满 + 交付树自洽（2026-10-13）

基于前两轮的 lint/CI/校验器/抽取器改动，本轮把“浏览器验收没有一条命令的入口”（T7）推到全绿，
并做了一次诚实自我评价（`SELF_EVALUATION.md`）。

- 后端 616 → **622 passed / 8 skipped / 0 failed / 122 subtests**（exit 0；JUnit `tests=752`）；
  前端 33 passed；`ruff check` 与 `format --check` 全绿。
- 浏览器验收：`ai_round.py --browser --ocr` 一条命令 **9/9 入口**、84 项断言、74 张截图
  （browser 11 项含真实中文 OCR、a11y 17、ui-polish 20、ui-review 7、feed-mode 6、
  source-live 8、source-selection 8、highlight-contrast 1、reliability 6）。
  唯一非绿项是 release gate：后端 suite 在轮次内重写 `backend.xml` 与运行日志的机器路径，
  按既定流程规范化后 `--check` 为 0（`0 file(s), 0 path(s) would be rewritten`）。
- 驱动器修复（先复现再修复，产品代码零改动）：feed-mode 的供稿种子与
  `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 信任、reliability 的 `--manifest`/`CAMPUS_E2E_FIXTURE`
  与“数据目录必须不存在”；契约以 `test_ai_round_browser_plan.py` 6 例钉住（含反向验证）。
- 交付树自洽：删除 4 个会随清单发货的根目录散落输出目录与空的 SQLite WAL 边车
  （主库 SHA-256 与清单记录一致，未动数据）；`RELEASE_MANIFEST.json` 重建为 763 项，文档三处数字同步。

以下为历史阶段验收记录，不应相加计数。

# 第 3.5 轮验收：自评后再提升——release gate 从"既定 FAIL"到真绿（2026-10-13）

SELF_EVALUATION.md 定稿后按"评价完再提升"再循环一次。自评标准第 2 条要求"一条命令复现且
全绿"，而第 3 轮的标准轮次是 4/5：release gate 每轮报 `2 file(s), 11 path(s) would be
rewritten`——后端 suite 在轮次内重写 `backend.xml` 与运行日志的机器路径，前两轮把这条记录
为"既定流程，规范化后为 0"。连续三轮付同一条人工命令的成本，说明它是缺陷，不是文档成本。

- **复现**：`ai_round.py` 无参一轮 → 4/5，唯一 FAIL 为 release gate（`--check` 报 11 处
  待重写）。
- **修复**（驱动器，产品代码零改动）：写证据的检查与 gate 之间插入 `normalize evidence`
  步骤（`normalize_evidence_paths.py --root .`，不带 `--check`）。pre-flight 不变——仍先跑、
  仍抓住"轮次前就脏"的树；新步骤只触碰本轮自己产生的证据，且作为独立步骤出现在汇总中。
- **钉住**：`test_ai_round_browser_plan.py::RoundFlowTests` 解析驱动器源码 AST，断言
  `check_backend` → `normalize_evidence` → release gate 的顺序及调用参数；反向验证：删掉
  `main()` 里的规范化调用，恰好该 2 例失败、其余 6 例不受影响。
- **结果**：稳态一轮 **5/5**（pre-flight / backend / frontend / normalize / release gate）。
  复跑第一轮 pre-flight 失败属正确行为：它如实报告上一轮未规范化的遗留。后端
  622 → **624 passed / 8 skipped / 0 failed / 122 subtests**（JUnit `tests=754`）；
  `ruff check` 与 `format --check` 全绿。未验证项与第 3 轮相同，无新增。

# 1.9 开发验收：独立复核与真实后端浏览器验收（2026-10-11）

基于用户提供的 `偷闲天大版-1.9-后端优化完成.zip` 继续开发，保留其任务1—10。本轮不改变产品语义，而是**独立复核**上一轮的三项后端改动，并把此前两轮一直标注"未验证"的真实后端浏览器端到端流程补上。

- 修改前基线：**562 passed、8 skipped、0 failed、106 subtests passed**。
- 本轮新增测试 25 例：`test_search_narrowing_corpus.py` 17 例、`test_review_gate_outbox.py` 3 例、`test_evidence_tree_hygiene.py` 5 例。
- 当前计数：**587 passed、8 skipped、0 failed、106 subtests passed**（203.53秒，exit 0；JUnit 共 701 个 testcase，其中 595 条为用例、8 条为跳过）；前端单元 **33 passed、0 failed**。

**独立复核（不依赖作者自己的夹具）：**

| 复核项 | 方法 | 结果 |
| --- | --- | --- |
| 索引收窄不改变结果 | 在**随包 73 条公开事项**的临时副本上，逐查询比较"索引收窄"与"全量扫描"两条路径 | 固定 11 个查询 + 全部标题汉字二元组，**结果完全一致**；并断言加速路径确实被走到，避免等价性空过 |
| 行与凭据不可分离 | 全量重建索引后，逐一检查"当前凭据必有对应行" | 通过；这正是提前跳过能够安全的原因 |
| 陈旧凭据不能隐藏命中 | 把凭据改旧并删除该事项索引行 | 回退为全量扫描，仍返回同一结果 |
| 遗留自动接受不能发布 | 构造 1.3 提取器留下的 **outbox 行**（不只是队列任务） | 派发后仍被 `candidate_not_accepted` 拒绝，无事项产生，outbox 清空 |
| 人工接受后同一路径仍可发布 | 正对照 | 发布 1 条，outbox 清空 |
| 拒绝决定不授权发布 | 审核为 `reject` | 无事项产生 |
| 读取不占写锁 | 复跑 `verification/backend/benchmark_reads` | 中位数 **167.5ms → 0.432ms**（另一写者持锁150ms，各9次） |
| 搜索加速 | 复跑 `verification/backend/benchmark_search` | `奖学金` **3.91×**、无命中 **4.91×**、`GPA` 1.00×、`报名` 1.12×；**5 组响应全部相等**；SQL 语句数（608→55、584→9）与公布值一致 |

**真实后端浏览器验收**（新增 `verification/browser/`）：9 个入口、**84 项断言**、74 张截图全部通过。每条入口都在**自己新起的回环合成服务**上运行，避免验证码 30 秒冷却与逐项状态互相耦合。

- `browser` 11 项（含真实中文 OCR）、`a11y` 17 项 axe-core 0 违规、`ui-polish` 20 项、`ui-review` 7 项、`feed-mode` 6 项（含供稿来源凭据）、`source-live` 8 项、`source-selection` 8 项、`highlight-contrast` 1 项、`reliability` 6 项。
- 全部无未捕获浏览器异常、无 HTTP 5xx。
- 报告只写仓库相对路径（见修复 4）。改完后复跑 `a11y` 复核：17 项审计、17 张截图、exit 0，`screenshots` 字段全部为 `verification/browser/a11y/artifacts/...`。

**本轮修复（4 处，均先复现再修复）：**

1. **移动端抽屉关闭后约 200ms 内仍可被 Tab 聚焦。** `ui-review` 的"关闭的侧栏不能接受 Tab"断言因此失败。该 CSS 与 1.8.0 **逐字节相同**，属**既有问题而非本轮回归**。修复：抽屉关闭且视口 ≤760px 时加 `inert`，立即退出 Tab 顺序，不依赖 `visibility` 过渡；`ui-review` 同时改为在断言前等待抽屉真正关闭。
2. **`a11y` 在入场动画结束前审计。** 卡片 `card-in` 动画 280ms，而 `stable()` 只等 2 帧（约32ms），axe 读到半透明文字的混合色，误报 12 处 `serious` 对比度违规。实测：等待点 12 处违规，动画结束后 **0 处**。修复：`stable()` 等待所有有限动画结束（截图路径早已用 `animations:'disabled'`，审计路径漏了这一点）。
3. **`ui-review` 关闭抽屉后 Shift+Tab 落到 skip-link，其聚焦态遮挡菜单按钮导致点击超时。** 这是上一条修复后暴露出的**正确**行为，测试补上显式释放焦点。
4. **交付证据里带着作者机器的绝对路径。** 浏览器报告、运行日志与 `backend.xml` 直接写入了运行时的绝对路径（含 `WorkBuddy AI` 工作区与解压目录）；其中被 JSON 转义的截图路径，正是 1.8.0 手工清理那 6 个报告时漏掉的一类。修复分三层：e2e 报告改经共享的 `relativeArtifact()`（`src/campus/tests/playwright.mjs`）写入仓库相对路径，仓库根由模块自身位置推出而非 `process.cwd()`；新增 `tools/normalize_evidence_paths.py` 规范化既有证据（前缀取自运行环境，不写死任何机器常量，幂等）；新增 `test_evidence_tree_hygiene.py` 五条用例钉住（前四条管路径，第五条管发布清单必须是 LF）。工具在构建过程中被自己的输出抓出四个缺陷——路径含空格时被截断、占位符里的尖括号破坏 XML、主机名规则匹配到自己的占位符导致不幂等、缺 NUL 字节的短二进制文件被当成文本解码失败——均已先复现再修复，过程记在 `verification/audit/path-hygiene.json`。同一次收尾还发现 `RELEASE_MANIFEST.json` 被重建成了 CRLF：`Path.write_text()` 会把 `\n` 翻译成 `os.linesep`，于是清单每一行都变，真正的改动被埋在整文件 diff 里。这个坑上一轮已记录过一次，这次又踩到，因此改为显式 `newline='\n'`，并用第五条用例钉住（先验证把清单改回 CRLF 会且只会让这一条失败）。

**重建产物**：`dist/` 与 `web/` 已按修复后的源码重建并保持逐字节一致；仅校园入口变化（`campus-ClSaRR9g.js` → `campus-CrW0rOa2.js`，+266 字节），桌面入口、CSS 与图片资源不变。

**无法验证：**8 项 PostgreSQL 用例需要隔离连接，本机无 PostgreSQL，仍跳过；无 docker/podman，未实机构建镜像；未连接真实 Meilisearch、云服务、SMTP 或生产队列；未做生产并发压测。上述性能为本机试验，不是生产 SLA。

**环境说明（与产品无关）：**本机沙箱对单次批量删除设有阈值，pytest 在会话收尾清理临时目录时会命中该阈值并中断进程，导致控制台摘要丢失（测试本身已全部执行完，JUnit 仍完整落盘）。因此最终计数用一次性 `--basetemp` 取得，输出为 `587 passed, 8 skipped, 0 failed, 106 subtests passed`，与 `verification/backend.xml` 的 `tests=701 / failures=0 / errors=0 / skipped=8` 一致。`verification/audit/backend-suite.log` 是这次运行的完整输出。**计数取自交付前的最后一次全量运行**：`test_evidence_tree_hygiene.py` 的用例是在上一轮运行之后逐条补上的（旧记录的 585 只覆盖 3 条），故重跑后记为 587。

以下为历史阶段验收记录，不应相加计数。

---

# 1.9 开发验收：后端审核、并发读取与搜索优化（2026-10-10）

基于用户提供的 `偷闲天大版-1.9-前端优化落地.zip` 继续开发，保留其已经完成的任务1—7。按单项顺序开发，每项先增加失败复现，再修复，再跑完整后端测试并单独提交。应用版本仍为1.8.0，冻结契约仍为1.5.0 / 59个操作。

- 安装：在仓库根目录执行 `pip install -r requirements-campus.lock.txt`，未新增或放松依赖。
- 修改前基线：**532 passed、8 skipped、0 failed、106 subtests passed**（42.63秒）。

| 本轮任务 | 修改的生产文件 | 新增测试 | 本项完成后的完整测试（通过 / 失败 / 跳过） |
| --- | --- | --- | --- |
| 人工审核关卡 | `content.py`、`events.py` | `test_manual_review_gate.py`，9例 | **541 / 0 / 8**，106子测试通过 |
| 数据库读取并发 | `db.py` | `test_store_reads.py`，12例 | **553 / 0 / 8**，106子测试通过 |
| 搜索一致性与性能 | `search.py`、`zh_index.py` | `test_search_consistency.py`，9例 | **562 / 0 / 8**，106子测试通过 |

生产文件均位于 `server/campus/`，测试均位于 `server/campus/tests/`。审核关卡还修改了既有 `test_pipeline.py`、`test_extraction_accuracy.py`、`test_data_audit.py`、`test_ingestion_backfill.py`：把自动接受的准备步骤改成真实人工审核或待审历史校验，没有放宽证据、时间或对象校验。

**修复与性能证据：**

- 提取仅产生 `pending / review_required=true`，清晰通知也不能自动接受；发布和旧任务回放必须找到候选相同、修订相同的 `accept` 审核记录。内部规则升至 `campus-rules-1.4`，显式重新提取才触发新待审修订，未批量处理随包历史数据。旧的无审核任务将拒绝发布，运营需在现有审核入口处理。
- 5类纯读取不再申请SQLite写锁；保留写事务内读取、异常整体回滚、PostgreSQL行锁和内存库互斥。在另一写事务占锁150ms的合成试验中，读延迟中位数 **180.063ms → 0.315ms**（各9次），只读取已提交内容。
- FTS命中集合与版本凭据同一快照读取，修复并发刷新导致的漏结果；确定无命中时在正文加载之前跳过。73个公开事项的数据库临时副本上，各7次预热后测量：`奖学金` **162.952ms → 45.582ms（3.57×）**，无命中 **182.488ms → 31.128ms（5.86×）**；五组搜索响应完全相等。广泛命中查询只约1.12×，ASCII子串仍全量扫描。详细SQL计数、脚本和计时见 `verification/backend/search.md`。
- 当前最终计数：**562 passed、8 skipped、0 failed、106 subtests passed**（42.32秒）；前端单元 **33 passed、0 failed**。`ShippedFrontendParityTests`在完整套件内通过。最终命令：`python -m pytest server/campus/tests -q -rs --junitxml=verification/backend.xml`。
- 字节核对：`campus-contracts/` 2个文件、`src/` 32个文件、`dist/`与`web/`各10个文件、`data/` 347个文件、`deploy/` 20个文件及9个依赖/来源配置文件与输入包完全相同。`dist/`和`web/`互相也逐字节一致。原样本数据库SHA-256保持 `629d8c61af1c39f0d4d1339d44266dbb18c961d0a76a06642be6fcf00b5bba78`。
- 两个Dockerfile继续从锁文件安装，云锁保留所有核心版本，Cloud Run本地预检6项通过。未新增GPL/LGPL或其他依赖，未操作线上服务或绕过采集限制。

**无法验证：**8项PostgreSQL测试需要隔离连接，本机无PostgreSQL服务或工具，仍跳过；无docker/podman，未实机构建镜像；未连接真实Meilisearch、云服务、SMTP或生产队列，未做生产并发压测与真实浏览器后端端到端测试。上述性能为本机函数/SQLite试验，不是生产SLA。保留1条既有Starlette/httpx弃用提醒，没有为消除提醒调整锁版本。

本轮日志、失败复现、性能脚本与逐项说明均在 `verification/backend/`。以下为历史阶段验收记录，不应相加计数。

---

# 1.9 开发验收：任务5—任务6（前端优化落地，2026-10-10）

上一阶段只改了前端源码、**没有重建产物**。本轮把那部分真正交付：重建 `dist/` 与 `web/`，并在真实 Chromium 中校验**已构建的字节**。应用版本仍为1.8.0，公共契约仍为1.5.0。下方任务4、任务3、任务2、任务1及原1.8记录为历史阶段结果，不计入本轮通过数量。

- 问题定位：上一阶段的 `src/campus/App.jsx`、`feedView.js`、`campus.css` 改动只停留在源码层（其原文自述为「仅源码，未重新构建」），而 `app.py` 服务的是 `dist/`，所以这些改动对使用者**没有任何效果**。`ShippedFrontendParityTests` 只比对 `dist/` 与 `web/` 是否一致，**不比对它们与源码**，因此测试全绿也发现不了这件事。
- 构建可复现性（重建的前提）：先用**改动前的源码**执行 `pnpm exec vite build`，产出与包内 `dist/` **10 个文件逐字节相同**（chunk 文件名与 SHA-256 全部一致）。这证明 `pnpm-lock.yaml` 钉住的工具链就是产出该产物的那一套，于是重建后的差异只能归因于源码改动，而不是换了一个打包器或版本。证据：`verification/task5/build-reproducibility.log`。
- 重建产物：`dist/` 与 `web/` 一起重建并保持逐字节一致。变化只落在校园入口——`campus-ClSaRR9g.js`（109,086 → 111,225 字节）、`campus-ZtFcd0uZ.css`（30,137 → 32,680 字节）、`campus.html`（资源引用改名）；桌面入口 `index.html`、`desktop-*`、`index-*.js`、`x-*.js`、`favicon.svg`、`touxian-icon.png` 均未变。`dist/` 合计 1,336,430 → 1,341,112 字节。
- 产物内证据：直接在交付的 `dist/` 字节里检索到 `今天截止`、`明天截止`、`天内截止`、`skeleton-card`、`tag urgent`、`偷闲 · 天大版`，确认新行为确实进了会交付的字节，而不只是留在源码树。证据：`verification/task5/shipped-bundle-markers.log`。
- 真实浏览器校验（新增 `src/campus/tests/shipped-bundle.browser.mjs`）：从 `dist/` 起本地静态服务，用 Chromium（Playwright 1.64.0 / chromium-1248）在 **1440px 与 390px** 两个宽度加载 `campus.html`，拦截 `/api/v1/*` 返回构造数据（截止日相对上海当天生成）。**32 项断言全部通过**：骨架卡片在首屏加载时出现；今天/明天/3 天内三档临近截止标签按上海本地日期正确出现，更远的截止不出现，今天档带 `.today` 类；已过期事项进入「历史通知」；`document.title` 随页面变为「今天 · 偷闲 · 天大版」「找信息 · 偷闲 · 天大版」；搜索命中标题的高亮包在**单个** `span` 内、行内连续（`maxGap = 0`），修掉此前被 `space-between` 撇到两端的显示问题；无未捕获页面错误与意外的控制台错误。证据：`verification/task5/browser-shipped-bundle.log` 与 `verification/task5/browser/` 截图。
- 性能实测（`verification/task5/perf-comparison.mjs`）：两处热点在改动前后**输出完全一致**（排序顺序、格式化文本），只是更快。`orderedCards` 75 张 2.75×、1000 张 3.08×、5000 张 3.36×；`formatStamp` **50.8×**（每次约 1.55 ms → 0.03 ms）——旧实现每张卡都新建一次 `Intl.DateTimeFormat`，75 条首屏约 100 ms 主线程开销。这两个数是**单机函数级微基准，不是端到端延迟结论**，也未做真机剖析。
- 完整测试结果：`python -m pytest server/campus/tests -q -rs`，**532 passed、8 skipped、0 failed、106 subtests passed**；前端单元 `node --test src/campus/tests/*.test.mjs`，**33 passed、0 failed**。`ShippedFrontendParityTests`通过。
- 顺带更正的既有漂移（非本轮引入）：`源码导航.md` 的模块行数与测试数量长期停在 1.8.0（例如 `content.py` 标 695 行、实测 1,696 行），本轮按实测更正；`verification/backend.xml`、`VERIFICATION.json`、`RELEASE_MANIFEST.json` 同步刷新。
- 未验证：**真实后端下的浏览器端到端流程**（本轮浏览器校验用构造数据，未连真实服务与数据库）；真机/低端机上的渲染性能与触摸行为；**镜像构建仍未执行**（本机无 docker/podman）。

本轮日志位于 `verification/task5/`；改动说明见 [UPGRADE_1_9.md](UPGRADE_1_9.md)。

---
# 1.9 开发验收：任务4（2026-10-10）

本轮处理依赖版本锁定。核查结论：**该要求已在 1.8.0 基线上满足，本轮不需要修改任何文件**，因此没有生产代码或部署文件改动，也没有新增测试。应用版本仍为1.8.0，公共契约仍为1.5.0。下方任务3、任务2、任务1及原1.8记录为历史阶段结果，不计入本轮通过数量。

- 现状核查：`git grep -E "pip install|COPY requirements" deploy` 显示 `deploy/` 下只有两处 Python 安装，且都从 lock 文件安装——`deploy/campus/Dockerfile` 用 `requirements-campus.lock.txt`，`deploy/cloudrun/Dockerfile` 用 `requirements-cloud.lock.txt`（后者首行 `-r requirements-campus.lock.txt`）。前端为 `pnpm install --frozen-lockfile`。未发现任何从 `requirements-campus.txt`（未钉版的范围依赖）安装的路径。
- 与基线逐字节比对：把本轮工作副本的 4 个文件（两个 Dockerfile + 两个 lock 文件）与**未改动的 1.8.0 原始包**（桌面 `偷闲天大版-1.8满分版.zip`）做 SHA-256 比对，**四个文件全部逐字节相同**。说明「从 lock 安装」不是本轮补上的，1.8.0 就已经如此；`git log -- deploy/` 也显示这些文件只在基线导入 commit 中出现过。
- 部署前置检查：`python deploy/cloudrun/preflight.py` 六项全过（必需文件、基础镜像 `@sha256:` 摘要、云 lock 保留全部核心版本、两份忽略文件的密钥排除、健康接口契约），`ok: true`，exit 0。
- lock 完整性：`requirements-campus.lock.txt` 为 **58 个钉版**、无嵌套 include、无非钉版行；`requirements-cloud.lock.txt` 为 **14 个钉版**并 include 核心 lock；合并后核心 **58/58 版本保留、无版本漂移**。
- 导入覆盖：扫描 `server/` 下 **58 个 .py 文件**的全部 import，**没有任何第三方导入不在 lock 中**——说明 lock 既没有漏包，也没有多余。
- 嵌套 `-r` 解析位置：在只含两个 lock 文件的目录（模拟容器 WORKDIR）执行 `pip install --dry-run --no-deps --ignore-installed -r requirements-cloud.lock.txt`，**72 个包（58 核心 + 14 云）全部解析出来**，证明核心 lock 被正确读到；另单独验证从其他目录用绝对路径指定时，pip 仍按**包含它的那个文件所在目录**解析嵌套 include，因此 WORKDIR 变化不会破坏安装。
- 未放松任何现有锁定版本：本轮未改动任何 requirements 文件或 Dockerfile，不存在放松。
- 完整测试结果（本轮未改代码，作为回归记录）：`python -m pytest server/campus/tests -q -rs`，**532 passed、8 skipped、0 failed、106 subtests passed**，139.56秒；1条既有 Starlette/httpx 弃用提醒。`ShippedFrontendParityTests`通过。
- 未验证：本机没有 docker/podman，**无法实际构建镜像**；也没有真实云环境，未实机执行 Cloud Run/Cloud SQL/GCS 部署。因此「在容器内成功安装」是静态核查（安装命令 + 锁文件解析 + 预检脚本）而非实机构建验证。

本轮日志位于 `verification/task4/`；改动说明见 [UPGRADE_1_9.md](UPGRADE_1_9.md)。

---
# 1.9 开发验收：任务3（2026-10-10）

本轮只做可读性重构，不改变行为；当时任务4尚未实施。应用版本仍为1.8.0，公共契约仍为1.5.0。下方任务2、任务1及原1.8记录为历史阶段结果，不计入本轮通过数量。

- 环境：新建 venv 后 `pip install -r requirements-campus.lock.txt`，**58 个锁定版本全部一致**，未新增或放松任何依赖。ruff 0.17.0 只装入该 venv 作一次性格式化工具，**未写入任何 requirements 文件**。
- 修改前完整基线：`python -m pytest server/campus/tests -q`，**532 passed、8 skipped、0 failed、106 subtests passed**，54.3秒。
- 格式化（独立 commit）：对 `content.py`、`delivery.py`、`events.py` 执行 `ruff format`。三个文件合计 **3670 行插入 / 1343 行删除**，纯版式。以 AST 摘要（`ast.dump` 且排除行列号属性）证明：三个文件格式化前后摘要逐字节相同，**语义不可能改变**。
- 重构（独立 commit）：`date_values` 由 230 行单函数拆为 **11 个小型命名函数**（`_split_time_clauses`、`_publication_anchor`、`_nearest_year_date`、`_literal_date_spans`、`_expand_range_endpoints`、`_append_weekday_spans`、`_append_relative_day_spans`、`_collapse_explicit_range`、`_clock_offset`、`_deadline_kind`、`_deadline_row`），`date_values` 保留为编排入口，**签名不变**；`content.py` 净变动 155 行插入 / 118 行删除。
- 行为等价证明：从上一 commit 取回旧实现，在**同一模块命名空间**内执行，对 **7,466 条文本 × 8 个发布时间锚点 = 59,728 次调用**逐条比对返回值（JSON 规范化），**0 处不一致**。语料为 `data/campus.sqlite3` 与 `fixtures/notice-corpus` 去重后的 7,432 条真实原文/证据文本，加 34 条覆盖各分支的手写边界用例。
- 修改后完整测试：`python -m pytest server/campus/tests -q -rs`，**532 passed、8 skipped、0 failed、106 subtests passed**。**未修改任何测试用例**。
- 换行符一致性：三个文件的换行符与输入包一致（`content.py`、`delivery.py` 为 LF，`events.py` 为 CRLF）；`dist/`、`web/`、冻结契约、原始快照、样本库均未改动；未触碰运行密钥、WAL、SHM。
- 未验证：真实学校学期日历、独立PostgreSQL并发/租约、浏览器端到端、OCR、生产外部服务及云部署。本轮未重建前端。

本轮日志位于 `verification/task3/`；改动说明见 [UPGRADE_1_9.md](UPGRADE_1_9.md)。

---
# 1.9 开发验收：任务2（2026-10-10）

本轮已完成任务1、任务2；当时任务3—4尚未实施。应用版本仍为1.8.0，公共契约仍为1.5.0。下方任务1及原1.8记录为历史阶段结果，不计入本轮通过数量。

- 依赖：沿用上一项已安装的 `requirements-campus.lock.txt`，本轮逐项核对版本全部一致，无新增依赖或锁定版本变更。
- 修改前完整基线：`python -m pytest server/campus/tests -q`，**472 passed、8 skipped、0 failed、106 subtests passed**，48.55秒。
- 八类短语链路检查：原实现均无补充解析；主提取器unknown占位值还会阻断补充链路。先写测试复现为 **39 failed、7 passed**，再实施规则及接入修复。
- 边界追补：相对年份/复合偏移先复现 **9 failed**；分钟去重及无发布时间入库先复现 **3 failed**。修复后的时间模块合计 **83 passed、0 failed**。
- 本轮新增 **60个测试实例**，全部在 `server/campus/tests/test_zh_time.py`；另修正1条旧测试：分钟精度应只填写at，date必须为null，遵循已有冻结契约。
- 修改后完整测试：`python -m pytest server/campus/tests -q -rs`，**532 passed、8 skipped、0 failed、106 subtests passed**，37.92秒；仍有1条既有Starlette/httpx弃用提醒。`ShippedFrontendParityTests`通过。
- 8项跳过均因未配置隔离的本地PostgreSQL测试URL；不是通过项。
- 未验证：真实学校学期日历、独立PostgreSQL并发/租约、浏览器端到端、OCR、生产外部服务及云部署。本轮未重建前端。
- 文件完整性：冻结契约、原始快照、样本数据库、dist/、web/与任务1包字节一致；未修改运行密钥、WAL、SHM文件。

以2026-10-10为发布日期时：

| 原文 | 解析结果 | 审核规则 |
|---|---|---|
| 元旦前 | 2027-01-01，date边界 | fixed_gregorian_holiday |
| 国庆节前 | 2026-10-01，date边界 | fixed_gregorian_holiday |
| 寒假前 / 暑假前 / 本学期末 | 无日期，unknown | term_relative_requires_term_start |
| 十月中旬 | 2026-10-11至2026-10-20，内部period | month_period_requires_review |
| 下月中旬 | 2026-11-11至2026-11-20，内部period | month_period_requires_review |
| 11月上旬前 | 2026-11-01，date边界 | month_period_requires_review |

所有内部推导结果都有needs_review=true，入库候选保持pending。旬范围经冻结Deadline接口投影为unknown，范围及原因保存到原有warnings字段；没有把中旬猜成15日。“前”表示日期边界，不猜前一日的截止时刻。无有效发布时间返回publication_anchor_required；节日/旬后的未支持复合偏移返回calendar_relative_requires_review。

本轮日志位于 `verification/task2/`；改动原因和精度投影说明见 [UPGRADE_1_9.md](UPGRADE_1_9.md)。

---

# 1.9 开发验收：任务1（2026-10-10）

本轮仅完成跨年日期修复，任务2—4尚未实施；应用版本仍为1.8.0，公共契约仍为1.5.0。下方原1.8验收记录是历史记录，不计作本轮验证。

- 环境：仓库根目录执行 `pip install -r requirements-campus.lock.txt` 成功，未调整依赖锁定版本。
- 修改前基线：`python -m pytest server/campus/tests -q`，**462 passed、8 skipped、0 failed、106 subtests passed**，1条既有Starlette/httpx弃用提醒。
- 先写失败测试：跨年新预期复现为 **5 failed、5 passed、32 deselected**，再修改生产代码。旧测试要求跨年返回unknown，按任务1明确要求更新为最近年份。
- 新增10个参数化测试实例：跨入次年、回溯上年、同年、截止区间、活动区间、闰日、183天保留、184天拒绝、无发布时间、无效日期。另更新1条既有测试预期。
- 阈值测试初稿把183天误写成拒绝；修正为183天保留、184天拒绝，生产代码阈值未变。修正后针对性检查 **11 passed**。
- 完整测试结果：`python -m pytest server/campus/tests -q -rs`，**472 passed、8 skipped、0 failed、106 subtests passed**，耗时40.81秒；1条既有弃用提醒。`ShippedFrontendParityTests`通过。
- 未验证：独立PostgreSQL并发/租约、真实校园网络、SMTP/推送、浏览器端到端、OCR及云部署。本轮未重建前端。
- 范围检查：冻结契约、原始快照、样本数据库、dist/、web/与输入包字节一致；未修改运行密钥、WAL、SHM文件。

本轮日志位于 `verification/task1/`；改动说明见 [UPGRADE_1_9.md](UPGRADE_1_9.md)。

---

# 偷闲·天大版 1.8.0 验收记录

产品版本 **1.8.0**，公共契约 **1.5.0**。本版在 1.7.0 的独立副本上修复时间解析正确性、恢复公开样本的复核决策链、清除作者机器绝对路径，并把前端双副本一致性变成可失败的检查。1.7.0 记录移至 [ACCEPTANCE_HISTORY.md](ACCEPTANCE_HISTORY.md)。

| 检查 | 本轮结果 |
|---|---|
| 校园后端 | **462 passed，106 个子用例 passed，8 skipped，0 failed**（JUnit 总计 576）；真实 SQLite、事务、HTTP 和队列；公网、SMTP、推送和 GCS 使用明确测试替身 |
| 新增回归用例 | **14 条**：`test_zh_time.py` 10 条（月份限定词、最近月份、带年份月界、限定月日、每月带「的」、学期相对、裸月日归属、发布时间锚点）+ `test_extraction_accuracy.py` 2 条（中文数字时刻、列举项不当时刻）+ `test_bootstrap.py` 1 条前端双副本一致性 + `test_release_sanitizer.py` 1 条决策链保留 |
| 本轮追补用例 | **17 条**：`test_platform.py` 3 条并发首次启动（见下「并发首次启动」）+ `test_search_index.py` 6 条（本地索引落后如实上报、摘录在句读处收口、窗口不越界、标题命中也能定位）+ `src/campus/tests/highlight.test.mjs` 8 条标记不变量；前端单元合计 **31 passed** |
| API 公共契约 | **59/59** 成功响应逐条校验，包括个人推送状态、当前事项证据质量、授权附件补充、就绪接口 |
| 前端边界 | **31 passed，0 failed**，包括 multipart、CSRF、幂等和隐私边界 |
| 生产网页 | Vite 构建通过，完整 `dist/` 与 `web/` 随包交付，两份拷贝逐文件 SHA-256 一致 |
| 真实浏览器 | **10 项基础流程 + 20 项布局/键盘 + 6 项可靠性 = 36 项通过**；明确合成数据，无接口拦截；未处理异常与 HTTP 5xx 均为 0 |
| 检索标记 | 对真实服务端命中核对：**2 条命中 2 处标记**，标记后的标题与摘录与接口返回值**逐字相同**；标记只加背景色，内边距与边框均为 0，不可能改变折行；浏览器实算对比度 **标题 10.33:1、摘录 4.96:1**（要求 4.5:1） |
| 无障碍 | axe-core 4.10.2，**19 个场景无确定违规**（17 个主场景 + 2 个可靠性场景）；渐变与弹层遮叠存在自动判断限制，不宣称完整 WCAG 达标 |
| 独立交付目录 | **6 项通过**：从独立交付目录启动、73 条历史事项接口与证据、无开发验证码、重启密钥与能力链接保持、交付数据库未被改写 |
| 发布清单 | **593 个文件**逐条大小 + SHA-256；`tools/build_release_manifest.py` 可重建清单，并对最终 ZIP 校验解压字节哈希、长度与 CRC，缺项/多项/改写即失败 |
| 公开样本 | 15 个来源、345 个不可变文档快照、2531 条证据、163 个候选、75 个事项（73 个当前公开、2 个撤回）；**103 条复核决定 + 2 条发布审计 + 78 次采集运行**随样本交付，actor 字段已移除 |

8 个跳过用例为依赖独立 PostgreSQL 的并发/租约测试，本机没有 PostgreSQL，**不是通过项**；`deploy/cloudrun/test_postgres_local.py` 需要单独核验的 PostgreSQL bin 目录才能执行。测试另有一条 Starlette/httpx 的弃用提醒。本轮浏览器未下载或执行中文 OCR 模型，报告内如实标注 `not run`；旧版 OCR 结果不计入当前证明。

授权附件弹层的两项自动判断限制另作了当前桌面视口的人工像素复核：普通文字对比度 13.19:1、链接 6.20:1，可见且有外跳标识。截图在 1.8 轮重新采集，三项测量的像素计数与对比度已在**新截图上重数并完全一致**，详见 verification/reliability/manual-a11y-review.json。

检索标记有一处**已知限制**，如实记录而不掩饰：标记只认**字面查询词**。服务端在字面命中过少时会用策展近义词补召回（例如「申请」扩到「报名」），这类命中在界面上**不会出现任何标记**，读者看不到它为什么被列出。排序本身已经把字面命中排在近义词命中之前（`test_search_index.py::test_near_synonym_widens_a_query_only_after_a_literal_search` 钉住这条），所以不会顶掉已有的字面结果；但要显示「为什么是这条」，需要接口返回命中词，而公共契约在 **1.5.0 冻结**，本轮**没有**为这一个显示效果去改动契约。实测例：查询「奖学金申请材料」返回 2 条，第 2 条「【演示】校园竞赛报名」即由近义词「报名」命中，无标记。

## 本轮修复的关键问题

1. **月份限定词被吞掉导致日期错月份**。原实现捕获了 `12月底` 的 `12` 却不使用，按发布时间求值，把 12 月写成 10 月；`十一月底`、`下个月初` 同样错，`下个月5号` 完全无结果。现在限定词被真正消费，纯月份数字取最近一次出现，带年份写法保留自己的年份。
2. **学期相对时间被当成发布时间**。`开学后两周内` 原按发布日 +14 天给出一个具体日期，参照点根本不同。现在报告为需要学期起点，不带日期，并把证据原文扩宽到「开学后两周内」。
3. **中文数字时刻丢失**。`下午三点` 原只得到日期、丢掉时间；现在与 `下午3点` 等价，同时排除「第三点」这类列举。
4. **公开样本丢失复核决策链**。整表删除 `reviews`/`publication_audits` 使「163 候选 → 73 公开」无法核对。现在保留决策与理由、移除 actor，并在样本清单中显式声明。
5. **交付物指向作者机器**。`verification/` 6 个报告 50 处、`backend.xml` 8 处、人工复核 3 处、8 个 e2e 入口的 Playwright 后备路径，全部改为相对路径或共享解析器。
6. **前端双副本可静默漂移**。`dist/` 与 `web/` 服务两类打包行为，都保留；新增逐文件 SHA-256 比对，只更新其中一份即失败。

## 并发首次启动（本轮追补的真实缺陷）

追查一条偶发失败的用例时，定位到的不是测试问题，而是**产品缺陷**：两个进程（或两个开发核心）同时打开同一个**全新**的 WAL 数据库时，先到者切换日志模式期间，后到者的 `BEGIN IMMEDIATE` 会被答复 `SQLITE_READONLY` 而不是 `SQLITE_BUSY`。`busy_timeout` 只覆盖后者，所以后者**直接启动失败**，而不是等待。原代码已经为同一类竞态给 `PRAGMA journal_mode=WAL` 加了重试，但紧随其后的建表事务没有保护。

- **修复前实测**：两次独立测量分别 **6/200** 与 **7/120** 次失败，报错全部是 `attempt to write a readonly database`。
- **修复后实测**：**120/120** 与 **180/180**，合计 **300/300** 收敛，无失败。
- **修法**：`Store._bootstrap()` 对这类瞬时竞争有界重试（60 次 × 50ms），并在两次尝试之间 `engine.dispose()`——因为输掉竞争的那条连接会**终生保持只读**，在原连接池上重试只是空转。非瞬时错误（如数据库损坏）不重试，仍然直接失败。
- **回归用例**：`test_bootstrap_retries_a_transient_readonly_race` 钉住「确实重试了一次」；`test_bootstrap_does_not_retry_a_real_failure` 钉住「损坏的数据库必须响亮失败」；`test_two_cores_open_a_fresh_database_together` 覆盖真实并发。把重试次数临时改回 1 可复现原报错，证明用例确实能捕获该缺陷。

## 样本和交付范围

随包 data/ 是用户提供的截至 **2026-10-09** 的历史公开样本，不能作为实时、附件齐全或全校覆盖的通知库。保留原始失败和采集时间，没有重新生成学校通知。公开通知中可能包含校内办理链接；只供学生浏览器打开，不授权服务器采集私网。

独立目录实检的 73 条当前公开事项中，**57 条存在已记录的文档或附件缺口**，详情会提示核对。这是历史数据的真实限制；授权补充流程提供处理方法，不伪造缺失附件或自动消除缺口。

独立清理程序以只读方式读取原数据并生成新数据库。账号、会话、验证码、个人待办与偏好、日历令牌、任务/发件队列、幂等结果和私密快照全部排除；`reviews` 与 `publication_audits` **保留但移除 actor 字段**，程序在写库前断言字段集合与 actor 缺失。公开证据的文件哈希、采集来源、段落引用和页码均核对；没有复制运行密钥、真实 .env、WAL/SHM、node_modules 或 Python 缓存。所有来源默认停用，启用前须逐个核验。

发行根目录 RELEASE_MANIFEST.json 列出全部 **593** 个交付文件的大小与 SHA-256，按路径分段排序；头部记录产品版本、契约版本、公开样本哈希，以及从哪个版本、哪个清单哈希派生（`derived_from_version` / `derived_from_manifest_sha256`），基线目录删除后仍可追溯来源。清单由 `tools/build_release_manifest.py` 生成并可反向校验：`--verify <zip>` 逐条比对解压字节的 SHA-256、长度与 ZIP 目录项 CRC，并报出缺失或多余条目；生成时若发现 `.server-secret` 或 `-wal`/`-shm` 会直接拒绝，而不是把它们写进清单。外部 .sha256 供下载后验证。verification/ 保存本轮报告和关键截图；截图来源是明确合成测试场景，不是学生数据或真实送达证明。

## 仍需上线环境验收

- Google Cloud 项目、Cloud Run 固定网址、Cloud SQL、GCS 与部署过程尚未实机执行。
- 真实校园网站出口、公众号授权与 feed、SMTP 收信、个人网关送达尚未联调；不绕过验证码或微信授权。
- PostgreSQL 并发/租约、线上容量与恢复、真实学生试用和实际节省时间尚未验证。
- 无障碍自动化、像素测量和键盘验收不能证明所有读屏软件、长正文与实际附件均符合标准。
- 时间解析的修复覆盖了本次列举的写法；中文相对时间表达没有穷尽，未覆盖的写法仍会进入人工复核队列，而不是被猜成具体日期。
- 并发首次启动的修复针对 **SQLite 全新数据库**。PostgreSQL 上的并发建表与租约行为仍属未执行（即那 8 个跳过用例），没有在本机验证；`pg_advisory_xact_lock` 路径只做了代码审查。
- 近义词命中在界面上不显示标记（见上「已知限制」），要消除它需要改动已冻结的公共契约。

本地结果支持继续做目标环境试运行，不能客观保证「100 分」或把尚未验证的能力算成已上线。运行方式见 [README.md](README.md) 和 [README-ONLINE.md](README-ONLINE.md)，具体变化见 [UPGRADE_1_8.md](UPGRADE_1_8.md)。
