# 真实可用性缺陷诊断（2026-10-18）

> 目的：把「能跑通」和「学生真的能用」之间的差距定位到文件与行号，并明确
> **哪些是真缺陷、哪些是设计如此**。后续修复（t2/t3/t4/t5）以此为依据。
>
> 诊断依据：本机经代理 TUN 访问真实天大官网（oaa / news / cic / gs 四个站点），
> 用产品自己的 `SafeFetcher` + `Pipeline` 跑通 采集 → 解析 → 抽取，
> 对产出的真实候选逐条核对。

---

## 结论速览

| # | 现象 | 真缺陷 / 设计如此 | 位置 |
|---|---|---|---|
| 1 | 标题抽成栏目标题「培养工作」 | **真缺陷** | `content.py:260` |
| 2 | 微信反爬页「环境异常」进候选 | **真缺陷** | `content.py:61`（识别不全）+ `sources.py` 重定向后未复核 |
| 3 | 截止时间 `raw_text` 是整段正文 | **真缺陷** | `content.py:655` |
| 3b | 同 kind 已有可解析日期仍留 unknown 行 | **真缺陷**（T5 覆盖面不足） | `content.py:706` |
| 3c | `value=None` / `precision=unknown` 行 | **设计如此**，不是缺陷 | `content.py:663-673` |
| 4 | 「提交电子版申请材料」返回 `[]` | **真缺陷** | `content.py:806` |
| 5 | 招生办法/考试安排被判 `other` | **真缺陷** | `content.py:1431-1446` |

---

## 1. 标题抽取取到栏目标题

**现象（真实数据）**
- `https://gs.tju.edu.cn/info/1111/17862.htm` → 标题得到 `培养工作`，正文首段是
  `各位同学：本学期非全日制研究生公共课考试时间见下表。`。该源 16 篇里有 4 篇标题都是「培养工作」。
- `https://oaa.tju.edu.cn/info/1059/9030.htm` → 标题正确。
- `https://news.tju.edu.cn/info/1003/563819.htm` → 标题正确。

**根因** — `server/campus/content.py:260`

```python
header = root.select_one("h1") or soup.select_one(".rich_media_title") or external_heading or soup.title
```

`root` 由 `CONTENT_SELECTORS` 选中（gs 模板命中 `#vsb_content`）。该容器**内部**包含页面头部元素，
其中一个 `<h1>` 是栏目名「培养工作」，`root.select_one("h1")` 命中的正是它，于是整篇通知的标题被替换成栏目名。

**影响面**：所有「正文容器内嵌栏目 h1」的模板。当前 19 个源里 gs 系列（研究生院 5 个栏目）稳定复现。
学生看到同一条标题的多条不同通知，无法分辨是哪一条 —— 这是**信息错误**，比缺字段更危险。

**修复建议**
1. 优先取 `<title>` 之外的**文档级**标题：`soup.select_one("h1")`（不受 root 限制）或
   `meta[property="og:title"]`、`meta[name="ArticleTitle"]`（常见 CMS 值），再回落到 root 内 h1。
2. 排除与**站点/栏目名**相同的候选：若候选项等于 `soup.title` 里的栏目段、或长度 ⩽ 8 且不含任何日期/动作词，
   视为栏目名丢弃，改用正文首行或 `<title>` 的主体部分。
3. 保留既有行为：文章页 `<title>` 常是「通知标题-天津大学教务处」，取第一段而不是整串。

**风险**：`<title>` 在部分站点含站点名后缀，需按分隔符（`-`/`_`/`|`）切分且只在尾部匹配站点名时切。
**守护测试**：`test_body_quality.py`、`test_extraction_audit.py` 覆盖正文质量，改动后必须全绿。

---

## 2. 微信反爬页进入候选队列

**现象（真实数据）** — oaa「通知公告」列表页 20 条里，有大量 `mp.weixin.qq.com/s/...` 链接。
采集后 `documents.source_url` 被改写成
`https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=...&target_url=...`，
解析出的文本为空，抽出的候选标题是 **「环境异常」**，8 条全部进了审核队列。

**根因**
1. `content.py:61 _html_interstitial()` 已实现访问页识别，但正则覆盖的是
   登录/认证/安全验证/人机验证/访问受限/access denied/just a moment/sign in/login，
   **不含微信的「环境异常」**，因此反爬页被当成普通文章。
2. `sources.py` 跟随 302 落到了 captcha 地址后，没有在写库前复核最终 URL 是否仍属于来源声明的文章模式。

**影响面**：凡是列表里混有公众号链接的源（oaa 三个栏目、教务/竞赛等）。垃圾候选会挤占人工复核队列，
运营者必须逐条手工拒绝 —— 真实使用时这是最大的时间黑洞。

**修复建议（不绕过反爬）**
1. 扩充 `_html_interstitial()`：加入「环境异常」「请在微信客户端打开链接」「完成验证后即可继续访问」
   等微信拦截页特征，命中即返回 `failed` + 新警告码（如 `html_wechat_interstitial`）。
2. 在 `sources.py` 入库前复核最终 URL：若发生重定向且落点不匹配 `article_url_pattern`，按 `FetchError`
   拒绝并记错误码，不产生快照与候选。
3. **明确不做**：不伪造 UA / cookie / 不请求 `poc_token` 兑换页 —— 仓库 N3 已否决绕过反爬。

**评估结论（来源配置）**：`config/campus-sources.json` 里 oaa 系的 `article_url_pattern`
同时允许 `tju.edu.cn/info/...` 与 `mp.weixin.qq.com/s`。建议**保留**该模式（公众号是官方发布渠道之一），
但在抓取层拒绝反爬页 —— 未来接入已授权 feed（RSSHub/WeRSS）时同一模式仍然适用。
不擅自删除任何来源。

---

## 3. 截止时间：`raw_text` 是整段正文

**现象（真实数据）** — 「天津大学智能与计算学部2026年博士研究生招生办法」产出 4 条 `school_deadline`：
```
[school_deadline] 2025-10-20 | 报名流程和要求等按照《天津大学2026年博士研究生招生简章》中规定的申请流程执行，请特别留意报名时间（第一阶段2025年10月20…
[school_deadline] None      | （同上，整段重复）
[school_deadline] 2026-03-10 | （同上）
```
`raw_text` 是整段长句，学生点「查看原文」无法定位到具体那句。

**根因** — `content.py:655`
```python
def _deadline_row(text, value, hour, minute, kind, evidence_id):
    ...
    return {"kind": kind, "raw_text": text, ...}
```
`text` 是调用方传进来的**整句/整段**，而不是命中日期的那一小段。`date_values` 里已算出
`tail`（日期之后到下一个日期或标点之间的片段），但没有把「日期所在的最短上下文」传下来。

**修复建议**
1. 在 `date_values` 中为每个命中计算**最短可核对片段**：以日期为中心，向左扩到最近的
   `，。；！？|` 或行首，向右扩到最近的 `，。；！？|` 或行尾，长度上限（如 80 字），
   作为 `raw_text` 传入 `_deadline_row`。
2. 保证片段仍逐字包含日期原文（证据链要求可通过 `evidence_ids` 回原文核对）。

**3b. 冗余 unknown 行** — `content.py:706 _is_heading_deadline_reference()`
T5 只丢弃「小节标题」形态的 unknown 行（需同 kind 已有可解析日期）。真实数据里
「报名流程和要求等按照…」这种**正文长句**同样没有日期，却因含「报名」触发了 cue，
产出第二条 unknown。建议把 T5 的「同 kind 已有可解析期限时丢弃无日期的 unknown 行」
扩展为**不限标题形态**，但保留 `DEADLINE_CLAIM_WORDS`（「另行通知」「截至」等）作为真实陈述豁免。

**3c. 设计如此（不要动）** — `_deadline_row` 中 `value is None` ⇒ `precision="unknown"`、`at=date=None`。
这是有意的：无法定精度就**不猜**，对应不变量「相对时间不得静默锚定到发布时间」。
`24:00` 归一为次日 `00:00`（T2）同样是正确行为。

---

## 4. 材料抽取漏掉「申请材料」

**现象（真实数据）**
`报名截止：报名学生需于2026年4月15日前提交电子版申请材料。` → `material_values()` 返回 `[]`。

**根因** — `content.py:806`
```python
if not re.search(r"申请表|报名表|确认表|成绩单|身份证|学生证|证明|简历|推荐信|陈述|方案|说明|作品", value):
    return []
```
白名单是**具体单据名**，「申请材料」是**材料类别**，不在其中。

**修复建议**：白名单增加泛化类别 `材料|材料清单|附件|电子版|扫描件|复印件`，
但保留 `content.py:800` 的排除项（`另行|待定|以.+为准|详见|见附件|见通知|暂无`）以免把
「材料另行通知」当成已确定的材料。变更后必须跑 `test_body_quality.py` 与
`test_extraction_accuracy.py`（含 96 项合成反例）。

---

## 5. 事件类型误判为 `other`

**现象（真实数据）**
- 「天津大学智能与计算学部2026年博士研究生招生办法」→ `other`（应为招生/报名，`activity` 或新增招生类）
- 「天津大学智能与计算学部2026年工程博士招生办法」→ `other`
- 「培养工作」（非全日制研究生公共课考试）→ `other`（考试安排，应为 `academic`）

**根因** — `content.py:1431-1446`
```python
event_type = next(
    (kind for words, kind in [
        ("奖学金|助学金", "scholarship"),
        ("竞赛|比赛", "competition"),
        ("就业|招聘|实习", "employment"),
        ("作业|论文", "assignment"),
        ("选课|考试|学籍|专业确认", "academic"),
        ("会议", "meeting"),
        ("活动|讲座|报名", "activity"),
    ] if re.search(words, title)),
    "other",
)
```
判定**只用 `title`**，而 gs 源标题被缺陷 1 污染成「培养工作」，于是永远落到 `other`。
「招生办法」不在任何词表里。

**修复建议**
1. 先修缺陷 1（标题），本缺陷的「培养工作→other」会自动大幅改善。
2. 词表补充：`招生|报名|申请|招录|考研|推免` → `activity`（或新增 `admission`，需同步契约，故本轮建议复用 `activity`）。
3. 判定输入从「仅 title」扩展为「title + 正文前若干段」，避免标题被污染时全线失明；正文权重低于标题。

**注意**：`event_type` 的取值集合受 `campus-contracts/domain-v1.schema.json` 约束，
**新增枚举值属于破坏性变更，本轮不做**。复用既有值。

---

## 修复顺序与依赖

```
缺陷1（标题） ──────────────┐
缺陷2（微信反爬页）           ├─→ 缺陷5（事件类型）依赖缺陷1 才能真实改善
缺陷3/3b（截止时间）          │
缺陷4（材料）                 ┘
```

四条互不改同一函数（1: `html_body_segments` 标题链；2: `_html_interstitial` + `sources.py`；
3: `date_values`/`_deadline_row`；4: `material_values`），可以并行修复，
但**合并时以最后一次全量回归为准**。

---

## 出厂门（不变）

```bash
PY="$(command -v python3)"   # 任何装齐依赖的解释器
mkdir -p .test-temp-rN/basetemp   # 父目录不存在时 pytest 会报 226 个 FileNotFoundError 假错
"$PY" -m pytest server/campus/tests -q --basetemp=.test-temp-rN/basetemp
# 当时的基线：680 passed / 8 skipped / 0 failed（最新数字以 verification/backend.xml 为准）
```

红测先行；改动后必须 0 failed；不破坏 `campus-contracts/`；逐块编辑（CRLF 风险）。
