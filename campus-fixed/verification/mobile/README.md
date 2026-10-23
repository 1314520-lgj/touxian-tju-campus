# 手机端 360 / 390 / 430 逐页验收（任务 t11）

## 结论

三档手机宽度下，五个核心页面都可以用真实点击走通，并且：

| 验收点 | 结果 |
|---|---|
| 横向滚动（`documentElement.scrollWidth <= innerWidth`） | 360 / 390 / 430 × 五个页面全部通过，`scrollWidth == innerWidth` |
| 真实浏览器逐页点击走通核心流程 | 通过，每档宽度 5 页截图 + 加入待办状态写回 |
| 首屏看到有效信息（带截止时间的事项，不用滚动） | 通过：三档宽度登录前后都能在首屏看到截止时间行 |
| 所有可点击元素 ≥ 40×40 CSS 像素 | 通过：三档宽度五个页面 + 桌面端 1280 七个页面，实测 0 个低于下限 |
| dist/ 已重新构建且与源码一致 | 通过：`npx pnpm@10.30.0 build` 退出码 0，产物哈希见下 |

桌面端 1280 同步回归：七个导航页全部无横向滚动、无小于 40×40 的触控目标。

## 怎么验的（真实浏览器，不是读 CSS 推断）

- 浏览器：本机 Edge 155（真实窗口，非无头），通过 browser-skill（`bsk`）驱动。
- 手机环境：`bsk emulate --mobile --dpr 3 --touch`，即真实移动视口 + 触屏 + 3 倍像素比
  （实测 `matchMedia('(hover: none)') === true`，`navigator.maxTouchPoints === 5`）。
- 每一档宽度都 `bsk reload` 后**用真实点击**走页面：点卡片标题进详情、打开抽屉点“我的事项”、
  点“添加事项”开表单、点“搜索”出结果。每一步都断言导航序号（`navIndex`），
  页面没真的切过去就报错（早期版本的脚本正是靠这条发现了选择器没生效、页面根本没切）。
- 度量：`measure.js` 由 `bsk evaluate` 注入页面，只读布局事实
  （`getBoundingClientRect` / `getComputedStyle`）：视口、`scrollWidth`、越界元素、触控目标、
  首屏信息。`measure.ps1` 把结果归档进 `measurements.jsonl`，`walk.ps1` 跑完一档宽度的五个页面。
- 视口高度取对应机型的真实值：360×780、390×844、430×932；另外补 375×667（iPhone SE 档）
  作为最矮参考，确认首屏判断不是只在“高个子”手机上成立。

## 修复清单（改前 → 改后）

### 1. 手机首屏看不到任何事项（P0，匿名与登录态都受影响）

改前（360×780，匿名首页，实测）：第一张卡片 `top = 725`，第一条截止时间 `top = 860`，
而视口只有 780 —— **必须滚动才能看到第一条带截止时间的通知**。
登录态（360×780）：第一张卡片 `top = 566`。

改后：匿名首页第一张卡片 `top = 505`、截止时间 `top = 640`；登录态第一张卡片 `top = 459`。
390×844 与 430×932 同样在首屏内（截止时间 `top = 643`，视口 844 / 932）。
375×667 也在首屏内（截止时间 `top = 643`）。

怎么省出来的（全部在 `@media (max-width: 760px)` 里，只压缩手机端）：

| 位置 | 改前 | 改后 |
|---|---|---|
| `.main-content` 上内边距 | 30px | 16px |
| `.page-heading` | `margin-bottom:26px; padding-bottom:22px; gap:17px`，内嵌 p 下边距 14px | `14px; 12px; gap:12px`，p 下边距 0 |
| `.welcome`（未登录的引导块） | `padding:28px 24px 26px; margin-bottom:40px; gap:24px; h2 28px` | `16px 18px 14px; margin-bottom:16px; gap:12px; h2 24px`；手机上隐藏其中的 eyebrow 与说明段（保留标题和“登录并设置我的画像”按钮） |
| `.section-title` | `margin:30px 0 17px` | `14px 0 9px` |
| `.stats` 三格 | 单元格 `padding:15px 12px 13px`，数字 30px | `9px 10px 8px`，数字 25px（360 档 23px） |
| `.topbar` 高度 | 60px | 56px |
| `h1` | 28px（≤360 档 26px） | 25px |

顶部横幅、日期、标题、副标题、筛选胶囊、卡片正文都没有删改，改动只是把首屏的“页面装修”
压紧，让内容顶上来。

### 2. 触控目标小于 40×40（全局）

改前（实测，390×844）：

| 元素 | 改前尺寸 | 说明 |
|---|---|---|
| `button.card-title`（卡片标题，进详情的唯一入口） | 307×**30** | 单行标题只有 30px 高 |
| `button.avatar`（顶栏头像） | **36×36** | |
| `button.login-top`（顶栏登录） | 65×**36** | |
| `.card-actions button`（编辑 / 删除） | **26**×44 | 两个字只有 26px 宽 |
| `.quality-document a`（打开原网页核对） | 116×**24** | |
| `.search-result a`（查看原文） | 70×**22** | |
| `.source-row a`（查看原站） | 70×**22** | |
| `.search-input input`（搜索框） | 202×**35** | |
| `label.check`（复选框行，按 label 计算命中区域） | 111×**24** | |
| `.filter-row button`（筛选胶囊，桌面） | 94×**34** | |
| `.card-actions button`（桌面） | 47×**36** | |

改后：新增一段“所有宽度都生效”的 CSS 下限（`src/campus/campus.css`），
`.card-title` / `.avatar` / `.login-top` / `.card-actions button` / `.filter-row button` /
`.search-input input` / `.text-button` / `.check` / 所有 `<a>`（这个应用里的 `<a>` 只有
外链、侧栏品牌和跳过链接，不是句子里的词）都不低于 40px。移动端另有 44px 的更高下限。

实测样例（390×844）：`.search-input input` = 202×**44**，`.search-result a` = 70×**40**；
探针统计从「9 个（匿名首页）/ 7 个（登录首页）/ 42 个（桌面首页）」降到 **0**。

> 桌面端注意：`filter-row button`、`card-actions button`、搜索框、`.check` 行、`.text-button`
> 在 1280 下也一并提到了 40px（改前 24–36px），卡片/表单会比以前略高几像素。
> 这是有意的取舍：让「不小于 40×40」这条在所有宽度都成立；视觉体系没有改动。

### 3. 顺带核对但没有改动的问题

- 360 宽度下 `.tabs`（详情页的页签、设置页与管理工作台的页签）本来就是横向滚动容器
  （设计如此：`overflow-x:auto`），窄屏下右侧页签被裁在容器里。探针把这类元素标成
  `clipped=div.tabs`，页面本身不横向滚动（`documentElement.scrollWidth == 360`）。未改。

## 实测数据

- `summary.md`：最终构建下每一档宽度、每个页面的测量表（机器生成）。
- `measurements.jsonl`：全部原始测量记录（含修复前基线，标签以 `before-` 开头）。
- 截图：
  - `after-00-today-anonymous-{360,390,430,se}.png`：未登录首页首屏。
  - `after-01-today-*` / `after-02-detail-*` / `after-03-my-*` / `after-04-add-*` / `after-05-search-*`：
    360 / 390 / 430 三档宽度的五个页面。
  - `after-02b-todo-{360,390,430}.png`：详情页“事项状态”改为待办之后。
  - `after-05-search-zh-390.png`：中文关键词“报名”的检索结果（28 条命中）。
  - `after-06…after-10-*-1280.png`：桌面端首页 / 详情 / 我的事项 / 搜索 / 我的设置。
  - `before-*.png`：修复前的基线截图（首页、详情、我的事项、搜索、添加表单）。

构建产物：`dist/assets/campus-CcCC4W4i.css`（sha256 前缀 CB98AD791B211375）、
`dist/assets/campus-BcOJCb7S.js`（ED979218C6473D74）、`dist/campus.html`（E044C66AA5386C20）。

## 契约里的两条验证命令

```text
npx pnpm@10.30.0 build                                   -> 退出码 0，dist/ 产物已更新
python scripts/verify_student_features.py http://127.0.0.1:8401 -> 退出码 0，21/21 项通过
```

另外跑了受影响的回归（前端改动不触碰后端逻辑）：

```text
python -m pytest server/campus/tests/test_bootstrap.py server/campus/tests/test_evidence_tree_hygiene.py -q
    -> 26 passed（含 dist/ 与 web/ 必须字节一致、证据树卫生两条）
node --test src/campus/tests/{boundaries,feedView,highlight,sourceSelection}.test.mjs
    -> 33 pass / 0 fail
```

`dist/` 重新构建后，`web/`（同一份构建的第二个副本）已同步为字节一致 —— 这是
`test_bootstrap.py` 里 `ShippedFrontendParityTests` 的硬性要求，不同步会让后端套件变红，
也会让只用 `web/` 的打包方继续发旧前端。`web/` 不在本轮声明范围内，属于“不改就会回归”的必要同步。

## 未验证 / 已知限制

- **没有真机验证**。验的是桌面 Edge 的移动视口仿真（dpr 3 + 触屏 + `hover:none`），
  真机 iOS Safari / Android Chrome 的滚动、地址栏收起、软键盘遮挡行为未验。
- **登录弹窗内部**没有单独跑探针：登录是真实点击走通的（取验证码、填码、提交，
  四次登录都在同一窗口完成），但探针只覆盖登录前的首页与登录后的页面。
- 375×667 首屏余量只有 24px（截止时间 `top=643`，视口 667）。更矮的视口（≤640）没有单独验证。
- 只逐页走了必验的五个页面 + 机会 / 信息来源 / 我的设置 / 管理工作台（手机 360 与桌面 1280
  各测一次）。管理工作台里的深层弹窗（审核、发布、接入申请处理）没有逐页走。
- 「加入待办」验证的是详情页“事项状态”下拉写回并在刷新后仍为待办（`state=todo` 持久化），
  没有验证提醒推送链路（需要推送服务，本机 `available:false`）。
- OCR 截图识别（手动添加里的“选择截图本地识别”）未在手机上验：需要下载识别模型。

## 复现

```powershell
# 1) 起服务（单机自用档，固定 8401，避开队长占用的 8000）
$env:CAMPUS_ENV='development'; $env:CAMPUS_DEV_AUTH='true'
$env:CAMPUS_DATA_DIR='<repo>\data-operator'; $env:CAMPUS_ADMIN_EMAILS='operator@tju.edu.cn'
python -m server.campus_app serve --port 8401

# 2) 用 browser-skill 起会话并绑定浏览器实例，然后跑一档宽度的逐页走查
#    （脚本里已包含 emulate + reload + 逐页点击 + 测量 + 截图）
powershell -ExecutionPolicy Bypass -File verification/mobile/walk.ps1 -Session <bsk会话id> -Width 390 -Height 844 -Label 390

# 3) 只测当前页面（例如手动操作后复核）
powershell -ExecutionPolicy Bypass -File verification/mobile/measure.ps1 -Session <bsk会话id> -Tag my-check
```

脚本注意事项（都踩过，写在注释里了）：Windows PowerShell 5.1 用 ANSI 代码页读无 BOM 脚本、
并用 ANSI 编码去传原生命令行参数，所以这三个文件必须是纯 ASCII，选择器里也不能出现中文
（中文选择器会变成乱码并且静默匹配不到元素）；页面身份因此用导航序号 `navIndex` 断言。

## t15（团队任务号）增补：首屏两条缺陷的修复

t12 独立复核在匿名首页量出两条 medium 缺陷，t15 修复。改动只在前端
（`src/campus/feedView.js`、`src/campus/App.jsx`、`src/campus/campus.css`、
`src/campus/tests/feedView.test.mjs`），随后重建 `dist/` 并把 `web/` 同步为字节一致。
**下面第 2 条取代本节上方 t11 表格里「手机上隐藏其中的 eyebrow 与说明段」那行**（现在手机端整个 `.welcome` 都不渲染）。

### 1. 过期日期抢先显示（原第一张卡是 9 月 24 日）

原因：公开列表 `/api/v1/events` 按 `updated_at` 排序，前端 `orderedCards` 又按「最早期限」重排、
卡片取 `deadlines[0]`，把后端 `events.py` 的「下一个待办期限」口径改了回去。
现在前端复用后端口径：列表键 = 下一个未过期期限（无期限/全部过期排最后，稳定排序）；
卡片只列出**还能办理**的期限，全部已过时保留并配「已截止」标签。
规则集中在 `feedView.js`（`nextDeadlineKey` / `cardDeadlineList` / `deadlinesOpenFirst`），带 3 条 node 单测。

实测（匿名 360×640，`scrollY=0`）：第一张卡「天津大学医科秋季专场招聘会邀请函」显示 `10月22日 10:00`（未来）；
前 12 张的可见期限依次 10-22 / 10-25 / 10-30 / 11-05 / 11-13 / 12-01 / 12-12 / 12-30 / 2027-01-18 / 无期限…（单调递增）；
原先顶在最前的四六级通知现在第 7 位，显示 `2026-12-12（未注明时刻）`。登录态同一列表也一致。

### 2. 小屏首屏看不到截止时间

t11 之后截止时间行停在 640→663px（360×640 完全看不到，375×667 只剩 1px）。
现在 ≤760 宽度下匿名首页隐藏 `.welcome` 引导面板——t11 之后它只剩一句口号加一个 CTA，
而同一登录流程在常驻顶栏「登录」和抽屉「邮箱登录」都可用——`main-content`/`page-heading`/
`section-title`/卡片上内边距也各收几像素，没有隐藏任何其它内容。

实测截止时间行 `top=463 bottom=486`：

| 视口 | deadline 底边 | innerHeight | 结论 |
|---|---|---|---|
| 360×536（按真机可视高度算） | 486 | 536 | 可见 |
| 375×553（按真机可视高度算） | 486 | 553 | 可见 |
| 360×640 | 486 | 640 | 可见 |
| 375×667 | 486 | 667 | 可见 |
| 390×844 / 430×932 | 486 / 467 | 844 / 932 | 可见 |

六档同时保持：`documentElement.scrollWidth == innerWidth`、0 个 <40×40 触控目标、逐个控件命中测试无遮挡。
桌面 1280 未受影响（`.welcome` 在 ≥761 仍然完整显示）。

### 3. 顺带：手机「加入待办」下拉

`.inline-actions select`（事项状态 = 加入待办）原来在手机上只有 40px 高，现在 44px（360/390/430 实测 100×44）。

### 回归

```text
node --test src/campus/tests/{boundaries,feedView,highlight,sourceSelection}.test.mjs   -> 36 passed
python -m pytest server/campus/tests/test_bootstrap.py -q                                -> 21 passed
python scripts/verify_student_features.py http://127.0.0.1:8421                          -> 21/21，退出码 0
```

仍未验证（与 t11 相同，未新增）：真机 iOS/Android；桌面 1280 不适用首屏规则（首屏只按两档手机宽度验收）。