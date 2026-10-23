# AGENTS.md —— 给下一个接手的 AI 运营者

> 注：历史文档（AI 交接、更新日志、发布说明等）已统一移入 `docs/archive/`，下文引用的文档名均在该目录下。

本文件是仓库唯一入口。你是接手运营这个产品的 AI：先读本文件，再按它指的路去读
详细文档。不要从零摸索——前 9 轮已经把坑踩完并写在纸面上了。

## 这个产品是什么

「偷闲天大版」：天津大学校园通知聚合服务。后端 FastAPI（`server/campus/`），
前端 React/Vite（`src/campus/`、`dist/`）。把分散在官网/公众号的招生、考试、
讲座、大赛通知抓取、解析、抽取成结构化事项（截止时间、材料、入口链接），
人工复核后发布，支持检索与订阅推送。

对外 API 契约冻结在 **v1.5.0**（`campus-contracts/`）：**只许增补，不许破坏**。
任何一轮改动前后都要对照契约；抽取消规则当前版本 `campus-rules-1.12`。

## 上手 10 分钟（按顺序）

1. `docs/archive/AI_PROJECT_CONTEXT.md` —— 工程结构、运行方式、本机环境的坑、目录速查、
   当前基线、每轮工作方式。**最重要的一份**。
2. 跑一遍测试确认环境可用（命令见下文「质量门」）。
3. `docs/archive/AI_PENDING_TASKS.md` —— 任务池。每轮挑 1～3 个相关问题开干；
   T19（赛程日期无 cue）已在第 11 轮修完；T6（搜索命中不带终止状态）已在第 12 轮修完。
   剩下的 P1 是 T10，
   两条都要动接口形状（属 B 级），**先出方案再实施**。
4. `docs/archive/AI_IMPROVEMENT_LOG.md` —— 前 11 轮怎么做的、为什么。照这个纪律来。

## 质量门（每轮出厂前必过）

```bash
# 仓库根执行；解释器须装齐依赖（fastapi/uvicorn/sqlalchemy/pytest）
# 先建 basetemp 目录：父级不存在时 pytest 会在 setup 阶段报 226 个 FileNotFoundError 假错
mkdir -p .test-temp-rN/basetemp            # Windows: New-Item -ItemType Directory -Force -Path .test-temp-rN/basetemp
export PYTHONUTF8=1                        # Windows: $env:PYTHONUTF8='1'
#   不设它，子进程 stdout 会按 cp936 解码，2 个 prune 用例假报失败（第 19 轮实测）。
#   看到"只有 prune 相关用例红"时先查这个，别去改抽取逻辑。
"$PY" -m pytest server/campus/tests -q --junitxml=verification/backend.xml \
  --basetemp=.test-temp-rN/basetemp      # Windows 必须；原因见 CONTEXT 第三节
```

纪律（前 9 轮无一违反）：

1. **红测先行**：先写会挂的测试，再修。没有红测的修复不受理。
2. **改完必全套回归**，且必须跑在**将要出厂的那棵树**上。纯文档改动除外，
   但要在文档里写明「未动代码，沿用上一轮套件结果」。
3. **诚实记录**：没验证的就是没验证，`VERIFICATION.json` 的「已知未验证」清单
   只增不减地更新。
4. **提交信息写详细**：改了什么、为什么、证据在哪。这个仓库的 git log 就是
   项目的记忆，`docs/archive/AI_IMPROVEMENT_LOG.md` 与它一一对应。
5. 每轮结束更新 `RELEASE_MANIFEST.json` 并重新打包（见下）。

## 打包出厂

```bash
python tools/build_release_manifest.py    # 重建清单（当前 1018 个文件）
python tools/package_release.py --root . --out ../<发布包名>.zip
```

清单构建器**不读 `.gitignore`**，它有自己的排除/拒绝规则：拒绝
`deploy/operator/session.json` 与 `.server-secret`（含活凭证，出现即拒发），
排除 `data-operator/`、`deploy/operator/out/`。往仓库加新目录时同步更新
`tools/build_release_manifest.py`，别只改 `.gitignore`。

## 三种运行模式（serve_cloud.py）

| 模式 | 用途 | 要点 |
|---|---|---|
| `demo` | 默认。公开样例数据，开发登录码开 | 演示/验收用 |
| `live` | 公网真实部署 | **强制关闭开发登录码**；主机采不到校园源时配 `CAMPUS_EMBEDDED_WORKER=false` |
| `operator` | 本机运营（人工复核+中继采集） | 开发登录码开；数据目录 `data-operator/` |

环境变量：`CAMPUS_CLOUD_MODE`、`CAMPUS_EMBEDDED_WORKER`、`PORT`。
当前有一个只读公网快照实例（周期 1 真实数据，live 模式，2026-10-13 截止）。

## 真实采集的三条通路

1. **校园网直采**：主机能路由 `*.tju.edu.cn` 时，`live` 模式 + `campus-contracts`
   外的 `config/campus-sources.json`（25 条声明、其中 24 个内置官方源）直接抓。
2. **运营方中继**（主机不可达时的合规替身）：`deploy/operator/relay.py`，
   子命令 `stage/build/serve/register/report/decide`。运营方抓真实页面 →
   逐字 staged → 授权 loopback feed → 产品自己的 SafeFetcher 原样采集。
   **铁律：feed 只是传输，不是身份**——item URL 永远是真实官方 https 地址，
   证据链必须指向原始通知。完整契约读 `deploy/operator/OPERATIONS.md`
   （含四级验收阶梯：传输/采集/抽取/发布）。
3. **本机一键直采**（运营者日常入口）：`scripts/campus_collect.py init|collect`，
   直接调用产品自己的 Core/Pipeline，注册来源 → 逐源采集 → 解析 → 抽候选 → 打印统计。
   本机代理 TUN 把 `*.tju.edu.cn` 解析成 fake-IP 时，加 `--gateway-from-config`
   用 `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 显式放行官方 origin（**不要**改 SafeFetcher）。
   完整命令、错误码与幂等说明见 `docs/REAL-DATA-RUNBOOK.md`。

## 天坑（都咬过人，别再踩）

1. **Windows 临时目录**：`TMPDIR/TMP/TEMP` 对 `tempfile.gettempdir()` 无效；
   pytest 默认 basetemp 收尾清理会触发沙盒拦截器假报失败。唯一解法
   `--basetemp=` 指向仓库内新目录。
2. **CRLF**：Python `write_text` 会把整个文件改写成 CRLF。改文件用编辑工具
   逐块替换，不要整文件重写；`RELEASE_MANIFEST.json` 必须保持 LF。
3. **写端点双头**：所有写请求必须同时带 `X-CSRF-Token` 和 `Idempotency-Key`，
   缺一个就是 422 `idempotency_required`。
4. **dev 登录码限额**：6 次/邮箱/小时，且两次间隔 ≥30 秒。跑运维脚本前先让
   `AdminSession` 复用 `session.json` 里的 cookie（按 api 实例隔离、复验）。
5. **评审队列形状**：`GET /admin/reviews` 返回的是 **candidate**，主键是
   `candidate_id`（不是 `id`），决策 POST 到
   `/admin/reviews/{candidate_id}/decision` 且要带 `expected_candidate_revision`。
6. **搜索响应键是 `hits`**，不是 `items`。
7. **中文文件名**：`.sha256` 侧链按 UTF-8 字节写，别用 ASCII。

## 当前基线（第 24 轮末 · 团队封版，2026-10-21，真实可用，可复现）

- 测试：**908 passed / 8 skipped / 0 failed / 151 subtests**。
  数字实读 `verification/backend.xml`：`tests=1067 failures=0 errors=0 skipped=8`，
  2026-10-21 13:14 在**冻结的交付树**上跑，退出码 0，用时 326.865 秒
  （1067 = 908 用例 + 151 子用例 + 8 跳过）。8 例跳过全部是 PostgreSQL 用例
  （未提供隔离的 PG 测试库）。前端单测：`node --test src/campus/tests/*.test.mjs` 36 passed。
- 第 14 轮修的抽取缺陷（诊断见 `docs/DIAGNOSIS-REAL-USE.md`）：标题不再取栏目名；
  微信反爬页识别并拒绝；截止时间引文收窄到命中子句；`申请材料` 类别词可抽取；
  `招生/报名/推免/招录` 归入 activity 且真实标题不被正文偶发词覆盖；
  `宣讲会` 不再一律算就业。
- 第 14 轮新增的运营入口：`scripts/campus_collect.py init|collect`（真实初始化 +
  幂等日常采集）、`scripts/verify_real_use.py`（端到端验收，退出码 0 才算过）。
  真实可用指引见 `docs/REAL_USE_GUIDE.md`，采集运行手册见 `docs/REAL-DATA-RUNBOOK.md`。
- 第 14 轮真实数据端到端已跑通：2 源 25 篇真实文档 → 8 候选 → 4 发布 → 检索 12 命中，
  事项详情带 4 条截止时间 + 1 条材料。
- 首个真实运营周期已完成：2 中继源、4 真实页面、4 决策（3 收 1 拒）、
  3 事项发布、搜索全召回。证据链机器可验（feed 逐字节可重建、
  blob sha256 == content_hash、原文逐字归档）。
- 第 14 轮解决了「两个教务处竞赛栏目只发公众号」的问题：同批竞赛通知由
  eee.tju.edu.cn/tztg/tz.htm（新工科教育创新中心，秘书处设在教务处）以原生文章发布，
  已注册为第 20 个官方源（见 `builtin_sources()`），正文 4664 字、带附件、0 公众号链接。
  公众号反爬页仍一律拒绝，绝不绕过。
- 第 14 轮还跑通了一个完整的**运营中继周期**（`deploy/operator/relay.py`），把两条
  只存在于公众号的通知（四六级报名、AI+合成生物大赛）逐字取证后送进产品并发布：
  四级验收全过（传输 sha256 4/4、两个中继源 healthy、截止时间全部带引文、证据链指回原文）。
  证据见 `verification/relay-cycle-acceptance-2026-10-19.txt`。
  顺手修了 `AdminSession._restore()` 只校验 API 不校验 email、导致复用错身份 cookie 报 403 的缺陷。
- 第 14 轮把**学生真实会用的功能**逐个跑通（scripts/verify_student_features.py，22/22）：
  登录、画像、今日清单、事项详情+原文证据链、检索、提问、收藏、待办、个人事项、ICS 日历、提醒偏好。
  过程中修了两个真缺陷：①提问把「报名截止/考试/报送材料」当成期限冲突而拒绝回答；
  ②一条无关通知内部的冲突会让整个回答变空。现在只跳过冲突那条并如实说明，其余照常回答。
- 第 15 轮：**过期事项自动下架**（scripts/campus_collect.py prune，默认演练、--apply 落地）。
  真实数据上 200 条已发布里 76 条截止时间已过 → 下架后公开列表 124 条、清单前排不再出现旧通知。
  同时修了清单排序：原来按「最早期限」排，一条 9 月报名截止 + 12 月考试的通知会被那已过的 9 月排到第一；
  现在按「下一个待办期限」排（events.py 的 sort_key），卡片抬头也把未来日期放前面。
  日常流程：采集完再跑一次 prune，见 docs/REAL-DATA-RUNBOOK.md §4.1。
- 第 16 轮（回应「可以使用了吗」的实测）：真实跑了一次日常采集，发现两个静默丢内容的问题并修掉。
  ① 一条 11-13 到期的招聘通知被抽成「取消」——依据是免责条款「集团有权…调整、取消或终止个别岗位的招聘工作」；
  取消候选无关联原事项就永远发不出去，这条真通知无声消失。修法：整句识别「保留权利」与「选项并列」，
  规则升到 campus-rules-1.12 并重抽；修复后该通知已发布（可办通知 8→9），取消候选 2→1（剩下的是真取消）。
  ② 复核策略原来只问「有没有证据」，79 条里放行 78 条而只有 1 条还有未来期限。现在判三种结果：
  reject（错误页/栏目碎片/表格附件名/新闻稿/已过期/无关联取消）、accept（**仅当天仍可办**）、
  review（其余留给人，机器不再替学生决定看什么）。真实队列 249 条 → 接受 11 / 拒绝 129 / 留给人 109。
  由此产生一条不变量：只发布还有未来期限的通知，于是下架清理**无事可做**（prune 报「无需下架」），
  闸门与清理口径一致，日常运行不再空转。
  另修：reextract 把「同标题已有新版本」的重复行报成失败（8 条），现改为 superseded 并说明内容已覆盖。
- 第 17 轮（按「一个学生能不能真的用」实测出三个 P0）：
  ① **启动脚本造出的应用登不进去**——没有 .env 时 dev_auth 默认 false 且无邮件服务，
  登录接口直接 503。启动脚本的单机档现在补齐 development/dev_auth/数据目录/管理员邮箱
  （只填空缺、不覆盖已有配置，production 下拒绝运行），并在启动时打印登录邮箱。
  ② **已发布列表里还留着旧轮次的垃圾**——「【考试安排】2022」×3、「总体情况」、「下载中心」、
  裸链接、新闻稿。复核策略加固只管新候选，管不到已经发布的。125 条里 25 条不合当前策略，
  且全部没有未来期限 → 用 `Events.withdraw_unusable()` 下架，**带未来截止时间的一律不动**。
  标题规则收敛到 `server/campus/hygiene.py`，复核门与下架共用一份定义，避免漂移。
  日常一条命令 `campus_collect.py prune` 同时管过期和质量。
  ③ 重启持久性用真实杀进程验过（`scripts/verify_persistence.py`）：个人事项与收藏在重启后仍在。
- 第 18 轮（团队分工，把「能跑」推到「学生日常真能用」；证据目录见 `verification/`）：
  ① **日常一条命令** `scripts/daily_update.py`（采集 → 复核发布 → 下架；默认演练、`--apply` 写库；
     文件锁防并发；退出码 0/1/2；`--json-out` 机器可读摘要）。在**全新数据目录副本**上实跑两次：
     公开列表 7 → 7 条、0 个重复标题；注入坏来源后该源报 `unsafe_address`、其余 19 个来源照常、
     已有数据只增不毁（`verification/coldstart-t13/`）。
  ② **每天自动跑** `tools/install_daily_task.ps1`（`-Install/-Uninstall/-Status/-RunNow`）注册
     Windows 计划任务 `TouxianCampusDaily`：每天 07:30、`LogonType=S4U`（不依赖登录会话）、
     `IgnoreNew`（不并发）、`StartWhenAvailable=False`（关机不补跑）。**本机已真实注册**并跑过，
     日志在 `<数据目录>/logs/`（`daily-YYYYMMDD.log` / `.json` / `last-run.json`）。
     不想要：`-Uninstall`。
  ③ **手机端**：360/390/430 三档宽度 × 五个页面真实浏览器逐页走通，无横向滚动、
     0 个 <40×40 触控目标、首屏可见带截止时间的事项；另一名队友独立复核（自起服务/自写探针/自己点击）
     结论一致（`verification/mobile/`）。
  ④ **首屏两条缺陷已修**：卡片抬头不再显示已过期日期（按「下一个待办期限」排）；
     360×640 / 375×667 / 真机可视高度 360×536、375×553 首屏不滚动即可看到截止时间
     （独立复验见 `VERIFICATION.json` 的 `round_18_first_screen`）。
  ⑤ **冷启动验收**：发布包解压到全新目录 → 照文档启动 → 登录 → 真实通知（带未来期限与原文链接）
     → 加入待办 → 真实杀进程重启 → 待办与会话仍在。
  ⑥ **登不进去的启动脚本**（承第 17 轮的两条）：`start-campus.ps1` 曾因「无 BOM 的 UTF-8 + 中文字符串」
     被 PowerShell 5.1 按 ANSI 读而**完全无法解析**（11 个 parse error），现已修好：
     5305 字节、含 UTF-8 BOM、`sha256 fefe18c1863762cba4afe84fee36bb88fcb957a0f690070c8997c9676a170fdc`、
     `Parser::ParseFile` 0 error（打包后再从包里解出来核一次）。
  ⑦ **首屏默认不再空**：今天页默认视图给「全部相关信息」（不再默认「需要我行动」），
     紧急入口「需要我行动」带数量角标、有紧急项时高亮；空态按「库空 / 只剩已截止 / 显式筛选为空」
     三种情况分别说明原因与下一步（`verification/firstscreen/`）。
- 第 24 轮（团队协作 t18–t26 → 封版交付；交付记录 `verification/final/README.md`）：
  ① **本轮学生可见增量实测为 0**：`data-operator` 上跑一次完整日常更新，公开列表 100 → 100、
     新增可见 0 条（**事件版本上升不算新增可见**——t24 踩过这个坑）；全新安装首天可见 8 条。
     t26 注册 4 个学院栏目后，副本上第一次运行 100 → 104（+4 条新增可见、0 条失去）。
  ② **瓶颈是内容不是管道**（t26 实测）：可见事项里 >90 天的 63 条、≤7 天的只有 4 条、今天仍可办 9 条；
     每日真实增量 0 → 首次 5，稳态估计仍不足 1 条/天（0.02–0.15，推导见 `verification/freshness/README.md`）。
     人工队列 127 条（去重后 94 组）仍需人过，机器只自动接受了 1 条。
  ③ **收益在机制**：采集不再把带验证码附件的文章整条判失败（三个教务处栏目 6 条 backlog failed → completed）；
     退出码恢复报警意义（跳过 = 可选通道未启用，真失败仍退 1，`test_daily_exit_codes.py` 钉住）；
     地址与附件缺口补齐（yzb 附件按原地址取回）；抽取不再把「3月15日前后」当截止；
     复核工作台 `scripts/review_queue.py`（优先级排序 + 机器为什么不决定 + 带理由决策 + 受控批量）与去重就绪。
  ④ **内容绑定**：`verification/final/README.md` 记录 start-campus.ps1 / dist↔web 一致性 / 清单 / 包的 sha256，
     并复核 t24 三条结论在当前字节上是否仍成立（公开列表口径不变 = 100；匿名首屏第一条已变为
     《【2027博招】环境科学与工程学院2027级硕博连读研究生选拔的通知》；退出码语义不变）。
  ⑤ 四个新官方栏目（`builtin_sources()` 共 24 个，一个 origin 一个来源的不变量保持）：
     网络安全学院 `cse.tju.edu.cn/index/tzgg.htm`、理学院 `science.tju.edu.cn/index/tzgg.htm`、
     管理与经济学部 `come.tju.edu.cn/xwzx/tzgg.htm`、环境科学与工程学院 `tjusee.tju.edu.cn/xwzx/tzgg.htm`。
     研招网 yzb 虽然最活跃，但文章页没有独立标题（`<title>` 是站名、无 `<h1>`），
     **本轮不注册**（会给学生看站名），修法与理由见 `verification/freshness/README.md` 第 5 节。
- 文件清单 1018；抽取消规则 `campus-rules-1.12`；快照库/campus_records 等真实数据在
  `data-operator/`，**不随发布包出厂**；公网部署用的**干净快照**（副本擦除隐私集合）
  在 `deploy/snapshot/campus-snapshot-2026-10-16.sqlite3`，随发布包出厂。
- 公网实例状态：真实快照实例（`ec728a4b…`）在线但跑第 10 轮旧代码；demo 实例
  （`6923f20c…`）第 13 轮核验返回 400。第 12 轮发布包 + 干净快照 + 部署手册
  （`deploy/snapshot/DEPLOY.md`）已备好，**待运营方在有平台/可达主机的环境重发**
  （本机无 agentos 部署通道，第 13 轮未发布任何公网实例）。

## 边界（什么能碰，什么不能）

- 能：修 bug（红测先行）、补抽取消规则、做任务池里的项、改文档、加固发布卫生。
- 不能：破坏冻结契约；删/清/覆盖任何数据目录；把活凭证塞进发布包；
  为迁就不可达网络而放松 SafeFetcher 的地址安全判定（那是防 SSRF 的，
  拒得对；正确做法是走显式受信网关 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`）。
- 每次外部动作（部署、发信、公开数据）前停一下想清楚——参见 `SOUL.md` 纪律。
