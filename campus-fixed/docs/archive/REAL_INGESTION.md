# 真实接入：官网采集与公众号接入（运行手册）

状态：**链路已按真实页面核验，只差一台出网可达的主机**。2026-10-13 的三环境实测见
`verification/source-reachability-2026-10-13.json`：开发机（fake-IP 解析 + 代理不透传）与托管沙盒
均不可达；服务端抓取证明官网列表、官网文章、公众号文章三类页面真实可达且服务端渲染完整。
本文件是在可达主机上把「真实接入」跑起来的步骤与验收标准。

## 0. 前提：确认这台主机真的出得去

```sh
nslookup oaa.tju.edu.cn                 # 必须是全局地址；出现 198.18.x.x / 10.x / 127.x 即为 fake-IP，采集器会正确地拒绝
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://oaa.tju.edu.cn/bszy/tzgg.htm   # 期望 200
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw  # 期望 200
```

两条都 200 再继续。任一失败：这是主机网络问题，**不要**改产品网络安全检查（DNS 固定、
SSRF 防护、非全局地址拒绝都是设计行为）。

## 1. 官网真实采集（19 个来源）

```sh
# 用产品自己的受限采集器做一次探测（列表 + 1 篇文章 + 1 个附件）
python deploy/cloudrun/probe_sources.py --network --articles 1 --attachments 1
# 退出码 0 = 抽样来源全部通过；非 0 会打印 JSON 报告，逐条看 state 与错误码

# 全新数据目录注册来源并起服务（live 模式：开发验证码强制关闭，worker 常驻）
CAMPUS_CLOUD_MODE=live CAMPUS_DATA_DIR=./data-live PORT=8000 python serve_cloud.py
```

跑起来后按这个顺序验收（都有对应 API，管理员登录后可看界面）：

1. `GET /api/v1/sources` —— 19 个来源 `health` 变为成功、`last_success_at` 推进；
2. `GET /api/v1/admin/candidates`（或界面审核队列）—— 出现真实候选，字段带原文摘录；
3. 审核采纳后 `GET /api/v1/events` —— 出现已发布事项，详情含材料清单与截止时间；
4. `GET /api/v1/search?q=四六级` —— 能命中刚采集的真实通知。

已知真实页面特征（2026-10-13 服务端核验）：教务处列表混合 `/info/<数字>/<数字>.htm` 与
`mp.weixin.qq.com/s/...` 两类链接——配置文件已覆盖两种模式，`content.py` 支持 `#js_content`，
不需要改代码。

## 2. 公众号接入（三条合规路径，按推荐排序）

公共 `rsshub.app` 的公众号路由实测 403，**不能**用；产品也**不会**绕过微信反爬。三条路都要
「运营方控制的上游」，然后把真实 feed 地址注册成来源（`POST /api/v1/admin/sources`，需管理员
登录，界面会自动带 CSRF 与 Idempotency-Key）：

| 路径 | 适配器 | 运营方要做 | 模式 |
|---|---|---|---|
| 自建 [WeRSS](https://github.com/rachelos/we-mp-rss)（推荐） | `werss` | 部署后扫码授权、订阅具体公众号；`rss.full_context=true` 输出 `content:encoded` | `feed_content_mode=authorized_fulltext` |
| 自建 [RSSHub](https://github.com/DIYgod/RSSHub) | `rsshub` | 起实例并逐条验证路由真的返回正文（`?format=atom` 优先） | 同上，先核验再开全文 |
| 自有授权接口 | `authorized_api` | 保证 JSON 数组或 `{"items":[...]}`，带 `content_html` | `authorized_fulltext` |

最小配置（UUID 换成创建来源后返回的真实 `source.id`）：`config/campus-sources.json` 的
`sources` 数组按 id 精确匹配追加；`kind=wechat`、`adapter=werss`、`source_identity` 填已核实的
公众号标识，并把学校官网链接或单位确认记录写进核验依据。地址里**不得**放 token；受保护 feed 用
你自己的反代加 `Authorization: Bearer`。正文模式与 feed 格式差异见 `WECHAT_FEED_GUIDE.md` 与
`deploy/campus/ADAPTERS.md`。

验收标准与官网相同（来源健康 → 候选 → 发布 → 可检索），另外两条：条目链接指向微信原文章；
供稿失效时来源健康如实失败，不静默改用摘要。

## 3. 上线到公网

可达主机上照第 1 节常驻运行即可（那就是真运营实例）。若要沿用单端口托管：把该主机作为采集端，
`CAMPUS_DATA_DIR` 指向共享存储；本仓库的公网演示实例（demo 模式）只展示产品，不采集。

## 4. 运营方域名清单的实测结论（2026-10-13）

运营方提供 11 个官网首页与 16 个 `mp.weixin.qq.com/s` 文章链接，逐条服务端抓取判读，证据在
`verification/operator-sources-2026-10-13.json`：

| 域名 | 结论 |
|---|---|
| oaa / gs / job / scet | 已在册，本轮复测列表有效 |
| zs / yx / oaa 的两个补充栏目 | 本轮**新增 4 个来源**（招生网通知公告、迎新快讯、教务处通知公告总览、竞赛科创），目录 15 → **19** |
| www.tju.edu.cn / lib.tju.edu.cn / www.twt.edu.cn | 可达但没有可稳定解析的通知列表（首页聚合 / JS 单页 / 工作室站），未注册 |
| i.tju.edu.cn / zzzx.tju.edu.cn | 抓取失败，域名用途需运营方确认，未注册 |
| 16 个公众号文章链接 | 全部返回微信「参数错误」，不是真实文章；重新提供有效链接或改走第 2 节授权 feed |

单篇文章链接不作为来源注册（采集入口是列表页）；真实文章可解析性已由 2026-10-13 的教务处
四六级报名文章证明。

## 5. 明确不做的

- 不放宽 DNS/SSRF 检查让采集「看起来成功」（任务池 N4）；
- 不绕过微信公众号反爬、不用未授权登录态；
- 不把历史数据采集时间改成当前发行时间；
- 不宣称全校覆盖或全历史覆盖（发现窗口每轮最多 100 条，超窗历史需有界补采）。

## 6. 主机不可达时的运营中继（第 9 轮已跑通，2026-10-13）

当运营主机也出不去（本机与托管沙盒均不可达 `*.tju.edu.cn`），由**运营方本人**作为唯一
能触达官网的组件，中继把官方页面原样送进产品自己的授权 feed 管道。这不是放宽安全边界：
`CAMPUS_TRUSTED_GATEWAY_ORIGINS` 由部署管理员精确指定一个源（如 `http://127.0.0.1:8791`），
feed 条目 URL 仍然强制真实官网 https 地址（`feed_archive.original_url`），候选、证据、
发布事件全部指向原始公告。完整规程与四级验收阶梯见 `deploy/operator/OPERATIONS.md`。

第 9 轮周期 1 实测（证据 `verification/operator-cycle-1-2026-10-13.json`）：

- 2 个中继源注册后即 `health=healthy`、`verification=verified_official`；
- 4 篇真实原文（本科招生网章程与强基计划校考安排、教务处四六级报名通知、AI+合成生物大赛）
  采集→解析→抽取全成功；
- 人工审核 3 采纳 1 拒绝（强基计划因日程日期漏抽被拒，任务 T19）；`GET /api/v1/events`
  3 条 published，`GET /api/v1/search?q=四六级` 等召回全部 3 条，事件证据链接是真实官网/公众号原文；
- 完整性链可机器复核：staging→feed 逐字节可重建、快照 `sha256(blob)==content_hash`、
  中继原样正文完整出现在产品归档内（归档 = 产品固有 h1+published_time 外壳 + 中继正文）。

周期 1 同时暴露真实抽取缺口（T19：日程型日期「校考于6月30日…举行」不产出）——
这正是真实接线的价值：合成数据发现不了它。
