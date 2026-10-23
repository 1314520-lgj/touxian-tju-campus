# 首屏默认空态修复（t17）

## 0. 结论

冷启动后第一次打开，首页**默认给内容**：一个全新数据目录 + 真实采集的 7 条通知（其中 0 条「需要我行动」）
在默认视图里全部可见，不再出现「当前没有需要处理的信息」。「需要我行动」仍是第一颗筛选按钮，
带数量角标、有紧急项时高亮，点一下就看到那些事项。空库时空态说明原因与下一步，不再是"像坏了"的样子。

改的是前端（`src/campus/App.jsx`、`src/campus/campus.css`），已重新构建 `dist/` 并把 `web/` 同步为字节一致。
后端契约、`server/campus/events.py`、`scripts/`、`tools/`、`start-campus.ps1` 一律未动。

## 1. 改了什么，为什么

| 位置 | 改动 | 理由 |
|---|---|---|
| `App.jsx` Today 的 `useState('action')` → `useState('all')` | 默认视图 = 库里的全部未截止通知 | 冷启动实例有内容但没有 `required` 事项，把两者等同起来就是首屏空态 |
| `App.jsx` 抽出 `needsAction(card)`，同时算 `actionCount` / `reviewCount` | 「需要我行动」是被标记的子集，不再当作库的全部 | 数量角标让紧急入口在默认视图下依然可见 |
| `App.jsx` 筛选按钮行 | 三颗按钮都显示数量（0 不显示），有紧急项时「需要我行动」加 `needs-action` 类 | 「需要我行动」要显眼可用（验收第 2 条） |
| `App.jsx` 三态空态 | ①库空：说明来源默认停用 + 「查看信息来源」按钮；②库只剩已截止：指向下方「已截止或取消」；③显式筛选为空：说明这是筛选结果 + 「看全部相关信息」按钮 | 三种情况不该长一样（验收第 1、2、3 条） |
| `campus.css` `.chip-count` / `.needs-action` | 角标与高亮的样式 | 不新增任何纵向块，首屏高度观感不变 |

**为什么改前端而不是改后端默认**：后端的 `/me/feed` 返回的是完整清单与分类，没有"默认筛选"这个概念；
把"默认给内容"塞进后端的排序或过滤语义，会让 API 的返回依赖"调用者是谁、在看第几屏"，也会让
`需要我行动` 这个由画像派生出的前端视图变成契约的一部分（属破坏性改动）。前端改默认视图是同一产品
意图的最小落点，且不动 v1.5.0 契约。

**为什么不选"默认空态里再附一段最近通知"**：那等于承认默认视图给不出东西，学生第一眼仍是"一句说明"，
而不是"可核对的通知卡片"。

## 2. 验收对照

| 验收点 | 结果 | 证据 |
|---|---|---|
| 全新数据目录首次打开，默认视图不显示空态 | **通过** | 冷启动实例（全新数据目录 + 当天真实采集 7 条）默认视图 = `全部相关信息`（选中），7 张卡片可见，DOM 里没有 `.empty`；`measurements.jsonl` 的 `coldstart-1280x900` / `coldstart-360x640` 两行 |
| 默认与显式「需要我行动」区分清楚 | **通过** | 默认：`全部相关信息=true`、7 张卡、无空态（`01`/`06`）；显式点「需要我行动」：`emptyTitle=当前没有需要你行动的事项`、`emptyText=这是「需要我行动」筛选的结果，不是库空了：库里还有 7 条未截止通知…`、按钮「看全部相关信息」（`02`），点它回到 7 张卡（`03`） |
| 「需要我行动」入口显眼可用 | **通过** | 收藏一条真实通知后：角标变 `需要我行动 1`、类名 `needs-action`、边框色 `rgb(232,128,109)`（其余两颗为 `rgb(61,70,87)`），默认视图仍是 100 张卡（`08`）；点它 → 正好 1 张该事项（`09`） |
| 空库文案说明原因与下一步 | **通过** | 全新空数据目录登录后：`emptyTitle=还没有可看的公开通知`、`emptyText=来源默认停用，需要运营者启用采集后才会入库；入库后这里会按你的画像显示。`、按钮「查看信息来源」（`05`）；点按钮确实跳到「信息来源」页（同一会话内实测） |
| 360×640 首屏仍能看到带截止时间的事项 | **通过** | `vw=360 scrollW=360 hScroll=false`；三颗筛选按钮 92/126/87 × 40（≥40×40）；首卡「天津大学医科秋季专场招聘会邀请函」截止 `10月22日 10:00`，`deadlineTop=570 < 640`；全页 0 个 <40×40 目标（`measurements.jsonl` 的 `coldstart-360x640`，截图 `coldstart-360x640.png`） |

契约命令：

```text
python scripts/verify_student_features.py http://127.0.0.1:8431   -> 21/21 项通过，退出码 0
python -m pytest server/campus/tests/test_bootstrap.py -q         -> 21 passed（含 dist/web 字节一致）
全套回归（出厂树）：881 passed, 8 skipped, 0 failed, 132 subtests
前端单元测试：node --test src/campus/tests/*.test.mjs              -> 36 pass
```

## 3. 三个实例（怎么复现）

| 端口 | 数据目录 | 用途 |
|---|---|---|
| 8431 | 运营数据的**副本**（未写仓库的 `data-operator/`） | 契约脚本 `verify_student_features.py`；100 条真实已发布，验证「需要我行动」角标与高亮 |
| 8432 | 冷启动副本（当天真实采集、7 条、0 条 required） | 默认视图有内容 / 显式筛选为空 / 360×640 首屏 |
| 8433 | 全新空数据目录 | 空库空态文案 |

启动（模式与变量照 `docs/REAL-DATA-RUNBOOK.md` §3；端口换成上表）：
`CAMPUS_CLOUD_MODE=operator`、`CAMPUS_DATA_DIR=<上表>`、`PORT=<上表>`、`python serve_cloud.py`。
构建：`npx pnpm@10.30.0 build`，随后把 `dist/` 复制成 `web/`（两者必须逐字节一致）。

测量脚本：`powershell -ExecutionPolicy Bypass -File verification/firstscreen/measure.ps1 -Session <bsk 会话> -Port 8432 -Width 360 -Height 640 -Mobile -Label coldstart-360x640`，
结果追加到 `measurements.jsonl` 并落一张截图（`probe.js` 是它注入的探针，两者都是纯 ASCII）。

## 4. 未验证 / 限制

1. **JS 端到端套件（`src/campus/tests/*.e2e.mjs`）没有整套跑**（需要浏览器 runner）。它们用
   `getByRole('button', { name: '全部相关信息', exact: true })` 这类精确名字选择器，所以角标
   `<span class="chip-count">` 加了 `aria-hidden="true"`——可访问名仍是「全部相关信息」，不改变既有选择器语义；
   这一点是**按可访问名计算规则推断**的，没有用 Playwright 的角色引擎实测。
   前端单元测试（36 例）全过。
2. 没有在第二台机器上验；8431 用的是运营数据的副本，未在完整运营数据上跑日常采集闭环（t13 已覆盖）。
3. `--basetemp` 指向不存在的目录时，全套回归会报 226 个 setup error（`FileNotFoundError`）——这是 t13 记过的
   工具链坑，先建目录即 881 passed；本轮又踩了一次，建议文档里的命令改成先建目录或改用绝对路径。
4. 「库只剩已截止」那条空态文案（第 ② 种）**未在真实数据上触发**：需要一批已过期但未下架的通知，
   当前数据里没有。文案与分支来自代码审读，未实测。
5. 首屏 `deadlineTop=570`（360×640）是本次测量值；没有与改动前的构建做同数据 A/B 对比
   （改动未新增纵向块，仅筛选按钮内联角标），所以只能说明"改后满足"，不能给出前后差值。

## 5. 自审标注

- `campus.css` 的触控 ≥40×40 地板与首屏压缩是**我（t11）**的产物，本轮只做了复测，属自我复核。
- 「首屏能看到带截止时间的事项」这条不变量是**另一位队友（t15）**的成果，本轮复测独立于其自述。
- `scripts/verify_student_features.py`（21/21）是**队友（t14）**的验收脚本，我独立执行，未复用它此前的结论。

## 6. 证据清单

| 文件 | 内容 |
|---|---|
| `01-coldstart-default-1280.png` | 冷启动实例 · 登录后默认视图（7 张卡、无空态、`全部相关信息` 选中） |
| `02-coldstart-explicit-action-empty.png` | 显式点「需要我行动」→ 筛选结果为空的说明 + 「看全部相关信息」 |
| `03-coldstart-back-to-content.png` | 点回默认 → 7 张卡恢复 |
| `04-empty-anonymous.png` / `05-empty-loggedin-default.png` | 空库：匿名首页与登录后默认视图的空态（原因 + 下一步按钮） |
| `06-coldstart-default-360x640.png`、`coldstart-360x640.png`、`coldstart-1280x900.png` | 探针记录的两种尺寸（同尺寸重复截图，后者由脚本生成） |
| `07-rich-default-1280.png` / `08-rich-chip-count.png` / `09-rich-action-filter.png` | 100 条真实数据：默认视图、「需要我行动」角标+高亮、点它只显示那一条 |
| `measurements.jsonl` | 两行机器可读测量（360×640 / 1280×900） |
| `probe.js` / `measure.ps1` | 可复现探针（纯 ASCII） |
| `00-coldstart-login.png` | 冷启动实例登录后默认视图（与 `01` 同状态） |