# AI_PROJECT_CONTEXT —— 长期自主优化的项目上下文

> 这份文件是每一轮自动优化的**入口**。开始任何改动前先读它，再读
> [AI_PENDING_TASKS.md](AI_PENDING_TASKS.md) 与 [AI_IMPROVEMENT_PLAN.md](AI_IMPROVEMENT_PLAN.md)，
> 上一轮做了什么看 [AI_IMPROVEMENT_LOG.md](AI_IMPROVEMENT_LOG.md)。
>
> 目的只有一个：**不要每轮重新理解一遍项目，也不要推翻已经验证过的结论。**

最后更新：2026-10-15（第 11 轮：修 T19 日程型日期抽取）

---

## 一、这是什么

《偷闲·天大版》——把天津大学**公开通知**采集、抽取、复核、归并成「有依据的行动清单」的工程。

产品版本 `1.8.0`，冻结契约 `1.5.0`（59 个 API 操作）。上游是 [sssssjw11/touxian](https://github.com/sssssjw11/touxian)（MIT），
本仓库是独立的校园入口。

它**不是**已上线的学校官方服务，也**不是**演示壳子：`data/` 里是真实采集结果。

---

## 二、技术栈与运行方式

| 项 | 值 |
|---|---|
| 后端 | Python 3.13 + FastAPI/Starlette；SQLite（WAL）/ PostgreSQL；自研事务 `Store` |
| 前端 | React 18 + Vite 6；源码 `src/campus/`，构建产物 `dist/` 与 `web/`（**逐字节一致**，有测试强制） |
| 契约 | `campus-contracts/domain-v1.schema.json`、`openapi.v1.json` —— **冻结，改动需同步两端** |
| 浏览器验收 | playwright-core 1.64.0 + 内置 axe-core 4.10.2，9 个入口 |

### 本机执行环境（已实测）

> 下面的命令**不写死本机路径** —— 交付树里出现作者机器的绝对路径是一个已知缺陷，
> 有守卫用例（`test_evidence_tree_hygiene.py`）钉住。路径都用 `$HOME` 与相对路径表达。

```sh
# 所有命令都在【仓库根】执行；测试用 server.campus.* 绝对导入，进 server/ 里跑会 ModuleNotFoundError
# 仓库根 = 本文件所在目录

# Python：本机唯一带齐依赖的解释器（3.13.14 / pytest 9.1.1）
PY="$HOME/.workbuddy-ai/binaries/python/envs/campus19/Scripts/python.exe"

# 后端全量（必须 --basetemp 指向工程内新目录，默认 basetemp 会触发拦截器，原因见下一节第 1 条）
"$PY" -m pytest server/campus/tests -q --junitxml=verification/backend.xml --basetemp=.test-temp-rN/basetemp

# 前端单元
NODE="$HOME/.workbuddy-ai/binaries/node/versions/22.22.2-6/node.exe"
"$NODE" --test src/campus/tests/*.test.mjs
```

**浏览器验收**（每条入口必须**自己起一台服务**）：

```sh
export PLAYWRIGHT_MODULE="$HOME/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/AppData/Local/ms-playwright"

# 后台常驻；--data-dir 每轮换全新目录
"$PY" -m server.campus.tests.browser_demo --port 8015 --data-dir <全新目录>

export CAMPUS_E2E_URL=http://127.0.0.1:8015
"$NODE" src/campus/tests/ui-polish.e2e.mjs
```

`playwright-core` 1.64.0 装在 `$HOME/.workbuddy-ai/binaries/node/workspace/node_modules/`，
与机器上已有的 `chromium-1248` 匹配；仓库本身**不依赖**它（`src/campus/tests/playwright.mjs` 按
`PLAYWRIGHT_MODULE` → `playwright` → `./node_modules/playwright` 顺序解析）。

---

## 三、本机环境的坑（都已踩过，不要再花时间）

1. **pytest 的 basetemp 清理会在 Windows 上报假错。** 三种实测：① 跑完一轮再**复用**同一个
   一次性 `--basetemp` 目录，收尾 `rmtree` 失败，报出 151 个 `ERROR at setup`（假象，换目录即全绿）；
   ② 直接**用默认 basetemp**（CI 就是这么跑的）在收尾清理旧 `pytest-of-*` 历史目录时，若文件数超过
   沙盒批量删除阈值（本轮 275 个文件）会触发拦截器：把标记文本注入日志、并在 junit 写完后杀掉进程
   使 exit=1（junit 仍可作真实结果交叉验证）。
   ③ **第 9 轮更正**：`TMPDIR`/`TMP`/`TEMP` 环境变量在本机**不被 Python 的 `tempfile` 采纳**
   （实测 `gettempdir()` 仍返回系统临时目录），第 7 轮那条「把 TMPDIR 指向空目录」的偏方无效。
   **当前唯一实测有效的做法**：`--basetemp=<工程内新建空目录>`（如 `.test-temp-x/basetemp`），
   pytest 既不到系统临时目录清理、也不会触发拦截器；第 9 轮全套件以此法 exit 0 拿到完整摘要行。
   **注意父目录必须先 `mkdir -p` 建好**（第 10 轮实测：父目录不存在时 pytest 建 basetemp 报
   WinError 3，151 个依赖 `tmp_path` 的用例全部 setup 假错——先建目录再跑即全绿）。
2. **`data-dir` 不能复用。** 上一次跑到一半中断会留下半成品数据库，`browser_demo` 再起会报
   `source_disabled_or_missing`。**每轮用全新目录**。
3. **验证码有 30 秒/邮箱冷却、6 次/小时上限。** 多个入口共用一台服务时，后跑的会拿到 429 ——
   那是限流，不是产品故障。**每条入口一台服务。** 第 9 轮运营中继为此把会话 cookie 持久化
   （`deploy/operator/session.json`）：首次登录花一个验证码，后续子命令用 `GET /api/v1/me`
   验证复用；真被限流时最早的验证码滑出一小时窗口即每分钟恢复一个。
4. **`curl` 走系统代理会返回 502。** 探测本机服务要加 `--noproxy '*'`。
5. **`Path.write_text()` 在 Windows 会把 `\n` 翻成 `os.linesep`。** 生成 `RELEASE_MANIFEST.json`
   这类**必须 LF** 的文件时要显式 `newline='\n'`（已修在 `tools/build_release_manifest.py`，并有守卫用例）。
6. **git 已关掉行尾转换**（`core.autocrlf=false`）。项目**有意**让一部分文件 LF、一部分 CRLF
   （`RELEASE_MANIFEST.json` 必须纯 LF，有测试断言）。不要打开转换。
7. **强杀子进程后立刻删它的目录会偶发 WinError 32。** Windows 异步释放文件句柄：
   `taskkill /T /F` + `wait()` 返回后，`campus.sqlite3` 可能还被占用几毫秒，
   `TemporaryDirectory.cleanup()` 就会抛「另一个程序正在使用此文件」。
   `test_bootstrap` 的 `tearDown` 已改为有界重试（20 × 0.1s，最后一次仍失败才抛）；
   自己写会强杀子进程的用例时照这个模式做，别把竞态留给下一个人。

---

## 四、目录速查

```
AGENTS.md                 ★ AI 运营者唯一入口（第 10 轮）：先读它，再按它指的路读详细文档
app.py                    公开生产入口（导入无副作用；PORT→0.0.0.0；强制关闭开发验证码）
serve_cloud.py            单端口托管启动器（demo/live/operator 三模式，operator 见第 9 轮）
server/campus_app.py      FastAPI 装配
server/campus/            核心包（24 个顶层模块）
  content.py              ★ 正文抽取 + 日期/截止/材料抽取（最大模块）
  events.py               ★ 事件归并 + 发布/撤回
  search.py               检索与可解释排序
  zh_time.py              中文相对时间（补充，不覆盖主提取器）
  date_patterns.py        ★ 日期正则（被 content.py 动态 exec 加载）
  db.py                   存储层（含并发首次启动重试）
  platform.py             Settings / 存储装配 / 依赖注入
src/campus/               前端源码（App.jsx / api.js / campus.css / …）
src/campus/tests/         9 个 Playwright e2e + 4 个单测 + playwright.mjs 共享解析器
campus-contracts/         冻结契约
config/campus-sources.json  19 个天大官方栏目（新增须同步 sources.py builtins）
data/                     ★ 真实采集数据（SQLite + 不可变快照），**不进 git**
dist/  web/               前端构建产物（两份，有测试强制一致）
tools/                    build_release_manifest.py / normalize_evidence_paths.py / package_release.py
deploy/operator/          运营中继（relay.py + OPERATIONS.md + staging 证据；周期证据 registration/cycle-report）
REAL_INGESTION.md         官网与公众号真实接入运行手册（第 6 轮；第 9 轮增运营周期节）
verification/             验收报告、截图、JUnit（全部仓库相对路径）
```

---

## 五、必须保护的东西

### 数据（不得删除/清空/覆盖）

- `data/campus.sqlite3` —— 真实采集库：**345 文档 / 345 快照 / 163 候选 / 103 复核决定 / 75 事件
  （73 已发布、2 已撤回）/ 2,531 条证据 / 15 来源**
- `data/snapshots/` —— 不可变原文快照（345 份）
- `data/PUBLIC_SAMPLE.json`
- 用户数据：账户、画像、个人待办、收藏、已读、订阅与提醒、审核记录

**`data/` 不进 git**（28 MB）。改动前先备份到仓库外；任何库结构变化都要有迁移 + 备份 + 回滚方案。

### 功能与不变量

1. **`campus-contracts/` 是冻结契约。** 改 API 形状必须同步 schema 与 OpenAPI，否则契约测试会红。
2. **抽取与发布分离。** 后台采集**只**写待审核候选，**绝不自动公开**。这是设计，不是缺陷。
3. **原文快照不可变。** 新快照只能追加；证据必须能回到原始快照。
4. **事件身份 = 一条通知，不是一个标题。** 同 URL 重发无条件合并；不同来源撞标题必须标题独立佐证，
   否则 `_pending_merge_review` → 降级 `under_review`。该标志每次入口复位，**不能跨候选泄漏**。
5. **检索响应键是 `hits`**（不是 `items`）。命中里的 `excerpt` 是原文**逐字切片**，
   前端只加背景色，不改字不重排 —— 高亮改动一个字符，引用就无法核对。
6. **中文索引是一元 + 二元切分**（不是 jieba）。任何 Han 子串（长度 ≥2）都含自身二元组，
   所以二元索引保证可召回。改分词前先读 `zh_index.py` 顶部注释。
7. **撤回是审计记录，不是删除。** 历史版本留在 `event_versions`，对外 `GET /events/{id}` 返回 404。
8. **投递的「接受」不等于「送达」。** 超时报 `unknown`，调用方不得盲目重发。
9. **相对时间不得静默锚定到发布时间。** 参照点不确定时（`开学后`、`第8周`）报 `unknown` 进复核队列，不猜。
10. **全新数据库的并发首次启动必须能等待。** `Store._bootstrap()` 有界重试 + `engine.dispose()`；
    **只重试瞬时竞争**，损坏这类错误照常抛出。
11. **日期必须「消费」而不只是「匹配」。** 只匹配不计算会返回**格式合法、内容错误**的日期，
    比不返回更危险 —— 见第 1 轮修复的 `2026.11.30 → 2026.11.3`。

---

## 六、当前基线（第 12 轮实测，可复现）

| 项 | 数值 |
|---|---|
| 后端 | **680 passed / 8 skipped / 0 failed / 130 subtests**（exit 0，111s；JUnit `tests=818`，failures/errors=0；第 12 轮在出厂树上重跑，新增 `test_search_ended.py` 16 例 + 搜索集成 1 例） |
| 前端单元 | **33 passed / 0 failed** |
| 浏览器验收 | **9 入口全过**（browser 11 项、a11y 12 页 0 违规、ui-polish 12 项、ui-review 7 项、highlight-contrast 2 mark、source-live 8 项、source-selection 8 项、feed-mode 6 项、reliability 8 项），`python tools/ai_round.py --browser --ocr` 一条命令跑通；第 12 轮刷新证据（此前停留在第 4 轮） |
| 来源目录 | `config/campus-sources.json` **19 个栏目 / 9 台官方主机**（第 8 轮按运营方域名清单新增招生网、迎新网、教务处总览、竞赛科创 4 个；i/zzzx 抓取失败未注册）。新增来源必须**同时**登记 `sources.py` builtins——seed 只认两侧一致的来源 |
| 交付文件 | `RELEASE_MANIFEST.json` 记录 **794** 项（交付时 752，第 1 轮 +6，第 2 轮 +3，第 3 轮 +2，第 5 轮 +2，第 6 轮 +2，第 7 轮 +1，第 8 轮 +1，第 9 轮 +19：运营中继工具与其规程、12 个暂存证据文件、2 个测试文件、周期 1 证据；第 10 轮 +1：`AGENTS.md`；第 11 轮 +1：`test_real_page_schedule.py`；第 12 轮 +2：`test_search_ended.py` 与更新的浏览器证据；第 13 轮 +2：`deploy/snapshot/` 快照与说明） |
| 接手入口 | `AGENTS.md`（第 10 轮新增）——AI 运营者唯一入口：上手路径、质量门、三种模式、中继铁律、天坑、当前基线、边界。人类维护者从本文件第六节读 |
| 真实运营 | 第 9 轮跑通**首个真实运营周期**（运营中继）：4 篇官网/公众号原文进产品全链路，3 条事件发布、1 条候选被审慎拒绝；证据 `verification/operator-cycle-1-2026-10-13.json`，规程 `deploy/operator/OPERATIONS.md` |
| 公网实例 | 两个：① `demo` 实例（第 5 轮上线）：`https://6923f20ced0940368b177758e4938cef.sg2.agentos-app.run`，**第 13 轮核验返回 400**（疑似下线/回收，未向托管方确认）；② **真实数据只读快照**（周期 1，live 模式）：`https://ec728a4b88f943a7b3f2bc39b7d22d52.sg2.agentos-app.run`，**第 13 轮核验仍在线但跑第 10 轮旧代码**（登录接口 503 不泄露验证码，数据截止 2026-10-13）。第 12 轮发布包与干净快照（`deploy/snapshot/campus-snapshot-2026-10-16.sqlite3`，副本已擦除隐私集合）已备好待运营方重发，部署手册 `deploy/snapshot/DEPLOY.md`。重新打包：`python tools/package_release.py --root . --out <zip>` |

**8 条跳过的用例**需要隔离 PostgreSQL，本机没有 —— 这是**已知未验证**，不是通过。

### 已知未验证（不要声称已覆盖）

- PostgreSQL 并发/租约用例（8 条跳过）
- 容器镜像构建（本机无 docker/podman）
- 真实 Meilisearch / 云服务 / SMTP / 生产队列
- 生产并发压测；性能数字均为**本机试验，不是 SLA**
- 真实校园采集：本机天大域名 DNS 返回代理 fake-IP，采集器**正确地拒绝连接**（不放宽检查来掩盖失败）；
  **托管沙盒实测**（第 5 轮 `live` 模式）：出网仅达平台自身端点与 pip 镜像，当时 15 个来源健康检查全部失败、
  采集 0 条 —— 真实采集必须放在可达校园网的主机，`CAMPUS_CLOUD_MODE=live python serve_cloud.py`；
  第 8 轮目录扩到 19 个来源，但**健康检查仍未在可达主机上跑过**
- `i.tju.edu.cn` 与 `zzzx.tju.edu.cn`（运营方清单内）：服务端抓取失败，域名用途待运营方确认，未注册
- 运营方提供的 16 个公众号文章链接：全部为微信「参数错误」页，不对应真实文章
- 公网实例的 SMTP 登录：未配置（`login_delivery` 为 development/unavailable），演示实例靠开发验证码进入；
  生产真上线按 `deploy/campus/.env.example` 配 SMTP 并做实际收信验收
- 托管沙盒数据是否跨重启/重新发布保留：由托管方决定，未核验
- 浏览器验收 9/9 为第 4 轮结果；第 5 轮未改产品代码故未重跑
- 微信公众号来源（3 个栏目）：产品**正确地拒绝绕过**反爬；第 6 轮服务端核验单篇
  `mp.weixin.qq.com/s` 文章可解析（真实账号「天津大学教务处」），但真实采集仍需可达主机或
  运营方提供的合规 feed（自建 WeRSS / RSSHub / 授权接口，见 `WECHAT_FEED_GUIDE.md`）
- 第 9 轮运营周期：发布事件截止时间的正确性是**运营方逐条读原文的人工结论**，无第二机器证据；
  operator 模式检索走本地 FTS5，MeiliSearch 链路本次未启用；强基计划候选的日程抽取缺口
  已由第 11 轮 T19 修好（`campus-rules-1.7`），但**仍待运营方下一周期重新采集后复核发布**，
  且第 11 轮三句落点是对**运营方暂存文本**的实测，不是官网实时页面；中继快照完整性链已机器
  复核（sha256/逐字节包含），但「暂存文本==官网所见」仍依赖运营方取证时的人工忠实

---

## 七、工作方式（每轮照做）

1. 读本文件 + `AI_PENDING_TASKS.md` + `AI_IMPROVEMENT_LOG.md` 末尾。
2. 从任务池挑 **1～3 个**相关问题，优先「用户收益高 + 风险低 + 改动小 + 可验证」。
3. **先写复现（红），再改，再全量回归（绿）。** 分不清是产品错还是测试错时，先分清。
4. 只提交**经过验证的独立改动**，不要把不相关的改动混进同一个提交。
5. 更新 `AI_IMPROVEMENT_LOG.md` 与 `AI_PENDING_TASKS.md`。
6. **无法验证的部分如实写出来**，不要用「应该没问题」代替证据。
