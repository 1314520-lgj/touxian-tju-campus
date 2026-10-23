# 本地合成演示浏览器验收记录
## 第 3 轮全量复跑（2026-10-13，一条命令）

`python tools/ai_round.py --browser --ocr` 一条命令跑通全部 9 个入口，**84 项断言**、74 张截图，
无未捕获浏览器异常、无 HTTP 5xx。每条入口仍由驱动器分配独立端口与全新数据目录；证据落在
`verification/browser/<entry>/artifacts/`。`browser` 入口显式开启 `--ocr` 复跑真实中文 OCR（11 项）；
a11y 17 项审计 0 违规（2 处 color-contrast incomplete 按既定策略移交 ui-polish 的像素级核对）。

本轮修的两处**驱动器**缺陷（产品代码零改动），均先复现再修复：

- `feed-mode` 422 `unsafe_port`：入口要在界面上创建指向夹具的来源，`create_source()` 依次跑
  `canonical_url()` 与 `resolve_public()`——非常规端口一律拒绝、127.0.0.1 不是全局地址，
  除非同一份 `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 显式信任。驱动器现在随入口设置
  `--feed-event-info`、`CAMPUS_E2E_FEED_ORIGIN` 与服务端 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`；
  此前缺 `CAMPUS_E2E_CAPTURE_EVENT` 还被静默跳过“已授权供货证据标注采集方式”一项，现在随种子事件一并启用。
- `reliability` 起不来：`reliability_demo` 的 `--data-dir` 必须**不存在**（存在即 `parser.error`），
  而 `mkdtemp` 恰好把它建出来；`--manifest`/`CAMPUS_E2E_FIXTURE` 也一并补齐。

两条入口契约已提取为纯函数 `entry_launch_plan()` 并用 `test_ai_round_browser_plan.py` 钉住。
复现：`python tools/ai_round.py --browser --ocr`（依赖本机 Playwright/Chromium；OCR 首次运行需联网下载模型）。

## 1.9 全量复跑（2026-10-11）

9 个入口在真实后端上一次跑通，共 **84 项断言**、74 张截图，全部无未捕获浏览器异常、无 HTTP 5xx：`browser` 11、`a11y` 17、`ui-polish` 20、`ui-review` 7、`feed-mode` 6、`source-live` 8、`source-selection` 8、`highlight-contrast` 1、`reliability` 6。

**每条入口使用自己新起的服务。** 验证码接口有 30 秒每邮箱冷却与每小时 6 次上限，共用一台服务时后跑的入口会拿到 429，看起来像产品故障其实是限流。驱动脚本为每条入口分配空闲端口与全新数据目录，跑完即销毁。

本轮据此修了两处**测试侧**缺陷，以及一处**产品侧**缺陷：

- `stable()` 只等 2 帧（约32ms），而卡片入场动画 280ms，`a11y` 因此在半透明帧上审计，误报 12 处 serious 对比度违规；等所有有限动画结束后实测 0 处。截图路径早已使用 `animations:'disabled'`，审计路径漏了。
- `ui-review` 在抽屉刚开始滑动时就断言"关闭的侧栏不能接受 Tab"，属竞态；改为等待抽屉真正离开 Tab 顺序。
- 产品侧：移动端抽屉关闭后约 200ms 内仍可被 Tab 聚焦（`visibility` 在过渡结束时才翻转）。CSS 与 1.8.0 逐字节相同，是既有问题。现在抽屉关闭且视口 ≤760px 时加 `inert`，立即退出 Tab 顺序。

`feed-mode` 需要一个显式受信的本地供稿来源：夹具在 `/rss` 与 `/website` 提供合成文档，服务以 `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 信任该来源，入口以 `CAMPUS_E2E_FEED_ORIGIN` 指向它。不访问学校或微信。

**报告不再写入绝对路径。** 每个入口原先把刚写出的截图绝对路径直接推进 `report.screenshots`，于是报告里带着运行机器的用户目录与解压位置；被 JSON 转义的那一批正是 1.8.0 手工清理时漏掉的。现在统一经共享的 `relativeArtifact()`（`playwright.mjs`）写入仓库相对路径，仓库根由模块自身位置推出而不是 `process.cwd()`，分隔符统一为 `/`。修复后复跑 `a11y`：17 项审计、17 张截图、exit 0，截图字段全部为 `verification/browser/a11y/artifacts/...`。既有证据里的绝对路径由 `tools/normalize_evidence_paths.py` 规范化，并由 `server/campus/tests/test_evidence_tree_hygiene.py` 钉住。

复现：先起 `python -m server.campus.tests.browser_demo --port <p> --data-dir <d> --feed-event-info <f>`，再以 `CAMPUS_E2E_URL`、`CAMPUS_E2E_OUTPUT`、`PLAYWRIGHT_MODULE` 运行各入口；`reliability` 改用 `server.campus.tests.reliability_demo` 并设置 `CAMPUS_E2E_FIXTURE`。



## 1.6 变化可见性 / 检索索引 / 无障碍

本轮新增独立入口 `a11y.e2e.mjs`：把 `vendor/axe.min.js` 中的真实 axe-core 4.10.2 注入页面并就地执行，不做路由拦截，也不在运行时联网。**引擎文件缺失会直接中止**，避免"零违规"其实是"零检查"。

判定策略：`serious`/`critical` 违规失败；ARIA 规则出现 `incomplete` 也失败（ARIA 总能判定，无法判定即标记含糊）；`color-contrast` 的 `incomplete` 按设计交给 `ui-polish.e2e.mjs` 的像素级对比度检查，报告中标注为 deferred。

17 个场景全部通过、0 违规：未登录首页、登录对话框、六个主页面、事项详情（默认与变化记录页签）、390px 移动端首页、移动端导航抽屉、管理台五个页签。浏览器为 Chromium 151.0.7922.34，报告与 17 张截图写入 `.campus-a11y-output/`。

本轮据此修复的标记问题：

- 弹窗标题由 `<header>` 改为 `<div>`，消除与顶栏重复的 banner 地标。
- 侧边栏由 `<aside role="dialog">` 改为 `<div>`，桌面端 `role="complementary"`、移动端 `role="dialog"`（`aside` 不允许 `dialog` 角色）。
- 空状态、来源列表、个人事项、管理台补齐 `h2` 层级，消除 `h1 → h3` 跳级；空状态标题视觉尺寸保持不变。
- `.welcome-graphic`、`.filter-row` 补 `role="group"`，不再在 `generic` 角色上挂 `aria-label`。

同时复跑既有验收：`ui-polish.e2e.mjs` 20 项、`browser.e2e.mjs` 11 项（含真实中文 OCR）、前端单元 22 项全部通过，无未捕获浏览器异常与 HTTP 5xx。

复现：`python -m server.campus.tests.browser_demo --port 8015 --data-dir .campus-a11y-demo`，随后 `CAMPUS_E2E_URL=http://127.0.0.1:8015 node src/campus/tests/a11y.e2e.mjs`。

## 1.1 来源选择与云部署版

本轮 22 项前端单元、11 项原流程真实浏览器（含中文 OCR）和 7 项新来源真实浏览器通过。新来源流程覆盖零选订阅、网站/公众号申请、第二学生隔离、运营连接/拒绝、整页刷新后的自动关注和手机键盘布局。另有 8 项显式 route-fixture 界面测试通过；它们只证明界面行为。

复现真实来源流程，在应用目录的干净开发终端运行 `python -m server.campus.tests.browser_demo --port 8015`，另一个终端设置 `CAMPUS_E2E_URL=http://127.0.0.1:8015` 后执行 `node src/campus/tests/source-live.e2e.mjs`。这个专用测试入口仅监听本机，强制本地 SQLite，关闭 worker，明确导入合成就绪网站/公众号来源；不会访问学校或微信。原流程入口为 `browser.e2e.mjs`，OCR 额外设置 `CAMPUS_E2E_OCR=true`。本机须预先安装 Playwright/Chromium，可用 PLAYWRIGHT_MODULE 指定模块。

当前版本报告和截图随包放在“本版验收记录”；没有云项目，真实 Cloud Run、GCS、Cloud SQL、学校采集和公众号授权未运行。下面保留此前版本记录。

验收日期：2026-10-08。服务为根集成分支前端构建 `campus-DFyD-XWD.js`，Chromium 151.0.7922.34，桌面 1440×1000、手机 390×844。服务 `http://127.0.0.1:8000` 必须返回显式合成演示标志。

`browser.e2e.mjs` 最终执行 11 项检查全部通过，浏览器未捕获未处理异常，服务未出现 HTTP 5xx。

- 公开首页展示 3 条明确标记的合成事项，桌面无横向溢出。
- 普通学生通过界面邮箱验证码登录，开发验证码由真实后端响应提供。
- 真实学院目录选择计算机学院，保存本科、2024 级、北洋园校区画像。
- 收藏事项，连续完成与重新待办均返回 200，并从服务读取确认最终 `todo` 状态；证据与签名快照文件可读。
- 全文搜索返回原文片段，问答展示有依据状态及原文引用。
- 群文字本地提取在确认前没有新增事项请求，默认 DTO 不包含原文，摘录为 null，精确日期保持日期粒度。
- Tesseract 真正下载 `chi_sim+eng` 模型并在浏览器识别合成中文 PNG；本地提取后确认表截止日期为 `2026-12-22`，摘录同意未勾选，全程无后台 API POST。
- 保存日历偏好，获取真实 ICS，确认包含个人事项，撤销后旧链接立即返回 404。
- 手机上登录、菜单、画像、搜索可用，无横向溢出，Escape 能关闭登录对话框。
- 管理员在界面撤回事项，公开详情 404 且搜索无该事项；随后通过界面恢复公开，详情重新 200。
- 汇总浏览器异常与服务错误为空。

已独立查看首页、个人画像、证据、问答、个人事项、OCR、提醒、手机与管理截图。文字清楚、表单可操作，没有发现页面横向溢出或内容重叠。长表单在对话框内滚动。

这次验收使用合成通知和显式开发认证。正式学校采集、公众号真实授权、生产邮件送达与 PostgreSQL/Meilisearch 运维可用性属于部署环境验收，不由此记录声称已经验证。

可复现命令见上级 README；完整运行输出 `browser-report.json` 与 10 张 PNG 保存在交付目录的“验收截图”中。真实 OCR 首次运行依赖模型下载网络，识别结果仍需用户确认。

## 整体检查与优化复验（2026-10-08）

本轮使用隔离的合成演示服务 `http://127.0.0.1:8002`，显式开发认证和 `.campus-audit` 数据。前端构建为 `campus-CXvl1kyc.js` / `campus-C7DehuIh.css`，Chromium 151.0.7922.34；桌面 1440×1000、手机 390×844。此前 8000 服务及首次交付验收记录保留。

本轮修复了已复现的界面问题：

- 画像保存返回 200 后，切换设置页签会显示旧学院；同一缓存问题影响订阅和提醒偏好。三个表单现在保存服务端返回值，切换后保持一致。
- 今日清单原先会把已经截止或取消的事项继续放在当前行动区。现在将其保留在可展开历史区；个人过期未完事项也保留单独入口。日期按北京时间判断，日期当天仍有效；缺失或不确定的日期继续展示以供核对。
- 后页事项操作后全局刷新会退回首屏。现在保留已加载的分页范围，减少重新翻页。收藏、完成、删除均有实际成功后的明确反馈。
- 登录对话框初始 Shift+Tab 可跳到背景页面。现在循环只包含可见控件；手机菜单支持键盘循环、Escape 关闭及焦点恢复，关闭的侧栏不会接受键盘焦点。

检查结果必须按类型区分：

- **20 项前端单元通过**：原有 15 项加 5 项北京时间、日期精度、未知日期、取消及多截止日期、个人过期分组边界。
- **11 项真实后端浏览器检查通过**（`browser.e2e.mjs`，未拦截 API）：公开清单、邮箱登录、真实学院目录、画像/订阅/日历偏好保存后切换页签、连续事项状态修改、依据快照、全文搜索与引用问答、个人事项默认不上传原文、真实本地 OCR、ICS 及撤销、手机布局、管理员撤回与恢复。检查汇总无未捕获浏览器异常、无 HTTP 5xx。
- **7 项浏览器 UI 回归通过**（`ui-review.e2e.mjs`）：使用明确标记的浏览器 route fixtures，检查过期与取消的折叠展示、后页操作保持范围、删除反馈、摘录同意撤销、手机菜单键盘、对话框循环与焦点恢复、退出后移除个人内容。这些只证明界面行为，不作为后端隔离或并发正确性的证据。

本地 OCR 实际启动 Tesseract worker，下载并使用 `chi_sim+eng` 模型识别合成中文图片，正确提取 `2026-12-22`；确认保存前没有后台 API POST，摘录同意默认关闭。首次模型下载仍依赖网络，识别结果需要人工核对。

本轮原始输出在工作区 `outputs/校园优化验收/`，与首次“验收截图”分开：

- `live-backend-ocr/browser-report.json` 与 10 张截图：最终 11 项真实后端全通过记录（含 OCR）。
- `ui-after/ui-review-report.json` 与 2 张截图：最终 7 项明确 UI fixtures 全通过记录。
- `baseline/` 与 `ui-before/`：保留优化前通过记录和已过期事项仍显示当前区的失败复现，不作为优化版通过证据。

已独立查看重拍后的今日历史折叠长页、手机当前清单、引用问答、OCR 长表单、手机画像与管理撤回截图。文字可读，无横向溢出或内容重叠；长表单在对话框内部滚动。长页截图在拍摄前归零滚动，固定导航位置正常。

复现时设置 `CAMPUS_E2E_URL` 和 `CAMPUS_E2E_OUTPUT`，真实 OCR 检查额外设置 `CAMPUS_E2E_OCR=true`；使用本机 Playwright 安装时可通过 `PLAYWRIGHT_MODULE` 指定模块。只对显式合成演示的回环服务运行。单元入口为 `boundaries.test.mjs` 和 `feedView.test.mjs`。

正式校园采集、公众号授权、生产邮件投递、PostgreSQL 与 Meilisearch 的运行可用性仍由部署环境验收。本轮浏览器结果不将合成数据等同于真实学校信息。

最终独立优化包追加验收：后端 131 项、前端 20 项；规则 1.1 材料修复后重新运行真实浏览器 11 项（含本地中文 OCR）。最新截图与 browser-report.json 位于发布包 `优化验收截图/final-standalone`，可核对日期与材料合答和简洁材料清单。
