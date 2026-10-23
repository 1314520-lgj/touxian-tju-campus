# 冷启动与日常闭环验收（t13）

从发布包解压到全新目录开始，只用文档写的步骤启动，走完
「打开首页 → 看到真实通知 → 进详情 → 加入待办 → 重启后仍在 → 日常更新无重复 → 坏来源不破坏数据」。

## 0. 一句话结论

**产品本身能用**：全新目录、照文档启动、登录、看到真实通知（带未来截止时间与原文链接）、
加入待办、真实杀进程重启后待办仍在、日常更新幂等不产生重复、注入坏来源后其它来源照常且数据只增不毁。
**但随包分发的那个 zip 落后于本轮产物**（缺 t9/t10/t11 的东西），而且
**`start-campus.ps1` 在 Windows PowerShell 5.1 下根本解析不了**、**一键脚本的首装依赖在本机会卡死 20 分钟以上**
——这三条是本次最值得修的问题，见第 2 节。

## 1. 怎么跑的（两个包，都解压到全新目录）

| 代号 | 来源 | 树路径 | 端口 | 用途 |
|---|---|---|---|---|
| **A** | 随包 zip `campus-fixed-realuse.zip`（2026-10-20 14:59）解压 | `<工作区>/campus/_t13-coldstart/shipped/campus-fixed` | 8531 | 学生一键路径（README：`run-demo.bat`） |
| **B** | 当前源码树按仓库自己的发布清单规则导出（见 `build_equivalent_package.py`） | `<工作区>/campus/_t13-coldstart/current/campus-fixed` | 8532 | 运营路径（`docs/REAL-DATA-RUNBOOK.md` §3 + §4.0），跑真实采集 |

两个树都是全新目录、都没有 `data-operator/`、都没有复用 `campus-fixed/`：
A 的数据落在自己的 `.campus-sample/`，B 的数据落在自己的 `data-operator/`。
**仓库自己的 `data-operator/` 本轮没有被写入**（它的增长来自 t10 注册的每日任务，与本轮无关）。

为什么需要 B：A 的 zip 里没有 `scripts/daily_update.py`（t9）、没有 `tools/install_daily_task.ps1`（t10）、
前端还是 t11 之前的构建，验收里的「日常更新」「坏来源」两条在 A 里根本无从下手。
B 不是重新打包（没碰 `RELEASE_MANIFEST.json`），只是用 `tools.build_release_manifest.iter_shipped`
——打包脚本使用的同一份「哪些文件该出厂」定义——把当前树复制成等价包：924 个文件，含 t9/t10/t11 的产物。

## 2. 发现（按严重程度）

### 2.1 随包 zip 落后于本轮产物（影响出厂）
`campus-fixed-realuse.zip` 里 **没有** `scripts/daily_update.py`、`tools/install_daily_task.ps1`、
`verification/mobile/`；`dist/assets` 还是 `campus-Cijd-lvv.js` + `campus-DmiF_3lD.css`（t11 之前的构建）。
后果：契约里的 `python scripts/daily_update.py …` 与 `… install_daily_task.ps1 -Status` 两条
verify 命令**在这个包里直接“文件不存在”**；学生装到的是没有每日更新能力、也没有手机适配的版本。
→ 需要 `python tools/build_release_manifest.py` 重建清单后重新打包（本轮 out of scope）。

### 2.2 `start-campus.ps1` 在 Windows PowerShell 5.1 下无法解析（11 个 parse error）
文件是无 BOM 的 UTF-8 且含中文注释/字符串；PS 5.1 用 ANSI 代码页读 BOM-less 文件，
`throw '创建 Python 环境失败。'` 被撕开成不闭合的引号。仓库副本与随包副本 SHA256 相同、同样失败：

```text
ParseFile start-campus.ps1 -> parseErrors=11
  line 15: 表达式或语句中包含意外的标记“'    if ($LASTEXITCODE -ne 0) { throw '”
  line 16: 表达式或语句中包含意外的标记“鍒涘缓”
```

实际调用时给出 `Missing closing '}' in statement block or type definition`，脚本一行都没执行。
→ 文档/AGENTS 里「本机一键单机档」这条路径在 Windows 上不可用（学生路径 `run-demo.bat` 是 .bat，不受影响）。
修法二选一：给文件加 UTF-8 BOM，或把脚本内容改成纯 ASCII（本轮不修，按约定只报告）。

### 2.3 一键脚本的首装依赖在本机会卡死（20+ 分钟无进展）
`run-demo.bat` 第二步 `pip install -r requirements.txt`（默认缓存指向 `d:\moveddata\devcache\pip\cache`，
约 870 MB、junction 挂载）实测：**20 分钟没有进展**——CPU 累计 617 秒、无 established 网络连接、
`.venv\Lib\site-packages` 仍然只有 pip 自己，且因为 Python 块缓冲连一行输出都没有。
PyPI 与两个国内镜像 HEAD 都是 200（约 1 秒），所以不是网络问题。
换成 `pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` 后
**3 分钟内装完，退出码 0**。→ 建议文档给一条「装不动就换源/关缓存」的提示，或让脚本带 `--no-cache-dir`。

### 2.4 冷启动后首页默认是空的（数据层，不是布局）
- A（随包历史样例）：登录后默认筛选「需要我行动」显示 **“当前没有需要处理的信息”**；样例的截止时间全部已过期
  （`/api/v1/events` 里 0 条带未来期限），真实内容要切到「待核实」或展开「历史通知」才看得到。
- B（等价包 + 真实采集）：同样是空——刚采集发布的 7 条真实通知全部落在「全部相关信息」里，默认筛选 0 条。

一个学生第一次打开，看到的就是一句「当前没有需要处理的信息」。这与 t11 修掉的「首屏没内容」是不同层次的问题
（那是布局把内容挤下去，这是筛选口径把内容藏起来）。

### 2.5 全新解压的包里跑套件会得到 226 个 setup error（工具链，不是产品缺陷）
`python -m pytest server/campus/tests -q --basetemp=.test-temp-coldstart/basetemp` 在**目录不存在**的全新树里：

```text
FileNotFoundError: [WinError 3] 系统找不到指定的路径: '…/.test-temp-coldstart/basetemp'
655 passed, 8 skipped, 226 errors
```

先 `mkdir .test-temp-coldstart/basetemp` 再跑同一命令：**881 passed, 8 skipped, 0 failed, 132 subtests passed**。
仓库里因为历次运行早就留下了 `.test-temp-*` 目录，所以这条一直没暴露。

## 3. 逐条验收结果

| # | 验收点 | 结果 | 证据 |
|---|---|---|---|
| 1 | 从 zip 解压到全新目录，只用文档步骤启动、不补环境变量，到达登录页并登录成功 | **通过（含一处偏差，见下）** | A：解压 876 文件 → `run-demo.bat 8531` → `/healthz` `{"status":"ok"}` → 首页 `偷闲 · 天大版` → 登录页出现开发验证码并登录成功（`coldstart@tju.edu.cn`），截图 `A-01/02/03`；B：按 runbook §3 启动 → 登录成功（`coldstart-t13@tju.edu.cn`，开发验证码 178907），截图 `B-01/02` |
| 2 | 首页看到真实来源的事项，至少一条带可核对截止时间与原文链接 | **通过（在 B）** | 7 条真实发布；示例「市教委关于举办 2026 年天津市大学生新工科实践创新大赛的通知」卡片显示 `2026-10-25截止（原文未提供具体时刻）`，详情「原文依据 (31)」含 `https://eee.tju.edu.cn/info/1024/3923.htm`，截图 `B-04/05`；检索「报名」7 条命中、首条带 `https://ico.tju.edu.cn/info/1011/5852.htm`，截图 `B-08` |
| 3 | 加入待办 → 关闭服务进程 → 重新启动 → 待办仍在 | **通过** | 详情页把「事项状态」写成待办（服务端回读 `state=todo`，截图 `B-06`）→ `Stop-Process` 杀掉 uvicorn（端口释放）→ 用同一条命令重启（新 PID）→ 浏览器刷新后会话仍有效、我的事项仍有个人事项「冷启动验收：把班级群通知记成待办」、重开该事项详情仍是 `state=todo`（截图 `B-11/12/13`） |
| 4 | 触发日常更新后，公开列表没有重复通知 | **通过** | `daily_update.py --apply` 跑两次：第一次发布 7 条；第二次「新增发布 3 条、核对已接受候选 9 条（已发布过的不会重复计入）」，公开列表 **7 → 7 条、0 个新标题、0 个重复标题**；库里 `events` 集合正好 7 条（每标题一条）。摘要见 `B-daily-1.json`、`B-daily-2.json` |
| 5 | 日常更新失败时报错可读、不破坏已有数据 | **通过** | 把「天津大学研究生院 · 培养办事流程」的 origin 改成 `https://broken-t13.invalid/notice/list.htm` 后重跑：该来源报 **`unsafe_address`**（可读错误码），其余 19 个来源照常（`20 个：成功 17 · 降级 2 · 失败 1`），退出码 1「有失败但未阻断」，复核/下架继续执行；数据只增不毁：`events=7`、`event_versions=9`、公开列表 7 条无重复，记录总数 5510 → 5958（新增的是健康来源的证据/快照/候选） |
| 6 | 全程命令、输出与截图写入 `verification/`；未验证项单独列出 | **通过** | 本目录：18 张截图、3 份日常更新摘要 json、契约命令输出、本报告与复现脚本；未验证项见第 5 节 |

**验收 1 的偏差（如实标注）**：文档的 `run-demo.bat` 第一步「建 venv + 装依赖」在本机无法在合理时间内完成（见 2.3）。
我改用 README 明确要求的 **Python 3.12** 解释器，按 `requirements.txt` 装进同一个 `.venv`
（命令见 2.3，加了 `--no-cache-dir` 与国内镜像），随后**回到文档路径**执行 `run-demo.bat 8531`
（脚本检测到 `.venv` 已就绪即直接启动）。除「依赖从哪来」这一步外，其余步骤与文档一致；
但没有在「干净的 pip 缓存机器」上复现过，所以这一条只能算**通过（含偏差）**。

## 4. 契约里的三条 verify 命令

```text
powershell -ExecutionPolicy Bypass -File tools/install_daily_task.ps1 -Status
    -> 退出码 0（在等价包 B 目录里执行）：状态 Ready / 下次运行 07:30 / 触发时间每天 07:30（错过不补跑）
       / 运行身份 Administrator（S4U）/ 最近一次执行摘要。
       注意：在随包 A 里这条命令会因文件不存在而失败（见 2.1）。

python scripts/daily_update.py --data-dir data-operator
    -> 退出码 0（在 B 目录里执行，默认演练、不写数据）：20 个来源、待复核 97 条、将发布 9 条、下架 0 条，
       并如实提示「受信网关：未设置（本机 fake-IP 环境下会得到 unsafe_address）」。

python -m pytest server/campus/tests -q --basetemp=.test-temp-coldstart/basetemp
    -> 先建目录再跑：881 passed, 8 skipped, 0 failed, 132 subtests passed（3 分 09 秒）。
       目录不存在直接跑会得到 226 errors（见 2.5）。
```

## 5. 未验证 / 已知限制

1. **没有在第二台机器上验**：D 盘、`%LOCALAPPDATA%`、junction 缓存这些前提与本机相同。
2. **venv 首装**（文档的默认路径）在本机跑不通，未能在干净 pip 缓存的机器上复现；
   只记录了「默认卡死 20 分钟」与「`--no-cache-dir` + 镜像 3 分钟成功」两个事实。
3. **A（随包样例）没有走完整条 UI 链路**：样例无未来期限，所以「详情 → 待办 → 检索 → 手动添加」
   改在 B（真实数据）上走完；A 只验证了启动、登录、首页、筛选切换、详情打开（截图 `A-01…A-05`）。
4. **A 的卡片截止时间渲染存疑**：A（t11 之前的构建）的 `.deadline` 文本是原始 ISO
   （`2024-05-14T17:00:00+08:00；2025-05-23T14:00:00+08:00`），B（当前构建）是
   `2026-09-30（未注明时刻）`。我只记录差异，未定位是哪个版本引入/修掉了它——需要的人可以对比两个 dist。
5. 后端深层行为（订阅推送、OCR 识别、管理工作台审核流）未验；它们不在本轮验收点里。
6. 「新增发布 3 条」但公开列表仍是 7 条：3 次发布没有产生新的公开标题（`events` 集合仍 7 条、
   无重复）。是「更新了已发布事项的版本」还是「发布后未进入公开列表」没有定论，作为观察项留给下一轮。

## 6. 自审冲突（必须标注）

本次验收里有三处用到了**我自己或本队成员写的东西**，它们的可信度要打折：

| 用到的产物 | 谁写的 | 本次是否独立验证 |
|---|---|---|
| `tools/install_daily_task.ps1`（契约 verify#1） | **我（t10）** | **不是独立验证**：只验证了「命令能跑、状态读得出来」；它自己装的任务/日志语义来自 t10 的结论 |
| 前端构建（B 的 `dist/`，验收 2/3/4/5 的界面证据） | **我（t11）** | **不是独立验证**：B 的页面就是 t11 的产物；只有 A（随包旧构建）是独立样本，但 A 只覆盖了登录与首页 |
| `scripts/daily_update.py`（验收 4/5） | t9（队友） | 是独立验证：我自己跑了 3 次（apply ×2 + 演练 ×1），没有复用 t9 的自测结论 |

与 t9/t10/t11 的结论不冲突：t9 说「第二次跑 0 快照/0 候选（幂等）」，我这边第二次跑仍有新增
（26 快照 / 15 候选 / 3 次发布）——因为两次运行间隔里来源确实发了新内容，而公开列表没有重复；
两者说的是不同的事，不构成矛盾。

## 7. 证据清单

| 文件 | 内容 |
|---|---|
| `A-01-home-anonymous.png` … `A-05-home-review-filter.png` | 随包 A：首页（未登录/已登录）、登录弹窗（开发验证码）、详情、切到「待核实」筛选 |
| `B-01-home-anonymous.png` … `B-13-after-restart-detail-todo.png` | 等价包 B：首页、登录、筛选、详情（原文依据链接）、加入待办、我的事项、检索、手动添加、真实重启前后 |
| `B-daily-1.json` / `B-daily-2.json` / `B-daily-3.json` | 三次日常更新的机器可读摘要（首次 / 幂等对照 / 坏来源） |
| `commands.txt` | 跑过的命令与关键输出（只保留可读摘要，不含构建机路径） |
| `build_equivalent_package.py` | 用仓库自己的发布清单规则导出等价包（复现 B） |
| 本文件 | 报告；第 5 节为未验证清单，第 6 节为自审冲突声明 |

复现：把 `campus-fixed-realuse.zip` 解压到任意新目录（A）；`python verification/coldstart-t13/build_equivalent_package.py <目标目录>` 得到 B；
然后按第 1 节的端口与命令启动。证据里的原始 stdout（含构建机绝对路径）按仓库门禁要求未随包保留，只留了摘要。