# 公众号与完整订阅正文接入指南

核对日期：2026-10-08。此版本准备了接入能力和验证材料；交付包不包含公众号登录态，也没有替任何学校公众号完成授权。

目标是让学生选择想看的信息源后直接获得有原文依据的行动卡。公众号采集由运营方维护的独立桥接服务负责，偷闲负责按授权读取订阅、保留证据、去重和提取；学生不需要提交微信账号或 Cookie。

## 从官方源码得到的接入选择

| 项目与核对版本 | 已核对的方法 | 本项目采用方式与边界 |
|---|---|---|
| [RSSHub](https://github.com/DIYgod/RSSHub/tree/1f5bc61c5409787cf564ddac21bca7e26f4fc862)，`1f5bc61c5409787cf564ddac21bca7e26f4fc862` | RSS/Atom/JSON Feed 多格式输出；可以把上游适配器与阅读端分开 | 独立服务经标准订阅协议互通。正文模式优先 `?format=atom`，适配器 `rsshub`；或 `?format=json`，适配器 `authorized_api`。必须逐个核实路由确实提供正文，格式本身不能证明内容完整或身份正确 |
| [we-mp-rss / WeRSS](https://github.com/rachelos/we-mp-rss/tree/126993c81a00466e9a6bbab041eef34ab27abe9c)，`126993c81a00466e9a6bbab041eef34ab27abe9c` | 独立保存公众号订阅和采集状态，生成带 `content:encoded` 的 RSS，提供授权失效提醒 | 推荐固定公众号的 `/feed/<feed_id>.xml?limit=100` 与适配器 `werss`，保留原始文章链接。桥接方扫码、续期和授权状态仍需自己维护；不能保证所有公众号可采或永久有效 |

RSSHub 的 [RSS 模板](https://github.com/DIYgod/RSSHub/blob/1f5bc61c5409787cf564ddac21bca7e26f4fc862/lib/views/rss.tsx) 将条目内容写在 `description`。偷闲不把这种字段直接视为完整原文；[Atom 模板](https://github.com/DIYgod/RSSHub/blob/1f5bc61c5409787cf564ddac21bca7e26f4fc862/lib/views/atom.tsx) 输出 `content`，[JSON Feed 模板](https://github.com/DIYgod/RSSHub/blob/1f5bc61c5409787cf564ddac21bca7e26f4fc862/lib/views/json.ts) 输出 `content_html`。输出格式由 [模板中间件](https://github.com/DIYgod/RSSHub/blob/1f5bc61c5409787cf564ddac21bca7e26f4fc862/lib/middleware/template.tsx) 的 `format` 参数选择。已有查询参数时使用 `&format=atom`，不要再加第二个 `?`。

WeRSS 的 [RSS 生成器](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/core/rss.py) 在 `rss.full_context=true` 时输出 `content:encoded`；[订阅接口](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/apis/rss.py) 接受 `limit`，但还会受 `rss.max_items` / `rss.page_size` 限制。核对版本的 Atom 输出使用扩展字段，JSON 输出使用普通 `content`，不作为偷闲的完整正文协议；此处优先 XML/RSS。

以上固定提交通过 GitHub 官方 API 和 `raw.githubusercontent.com` 核对。提交只是本次方法核对依据；没有把它们当成已验证可部署的生产镜像。

## WeRSS 桥接服务的最小配置

1. 在独立服务完成所需账号登录、扫码及公众号订阅。对所供内容取得适当授权，记录原始公众号身份与供稿范围。登录态保留在桥接端，不传给学生浏览器或偷闲前端。
2. 选择固定公众号的 XML 订阅，而不是把全部账号混成一个无法追溯身份的来源。`rss.local=false`，确保条目链接指向原始文章；`rss.full_context=true`、`gather.content=true`，开启正文采集与输出；`rss.cdata=false`，使用正常 XML 转义。核对版本把 `rss.cdata=true` 的标记放入 `Element.text`，会变成转义后的字面字符，不能据此假设是正确 CDATA。
3. 用实际输出确认有标题、原文链接、真实发布时间和非空 `content:encoded`。只有摘要或链接时保留链接模式。缺少发布时间时保持未知，不补成抓取时间。
4. 为桥接服务保留数据库、正文和授权状态的持久存储。上游会话与采集进程具有状态，不直接假设能放进按请求启动、随时缩容的无状态容器。偷闲主服务仍按交付的 Google Cloud 固定网址部署方案运行。
5. 固定经过自测的发布版本和容器 image digest，保留配置与数据备份；不要在生产中依赖浮动 `latest`。本包没有下载、扫描或运行第三方桥接镜像，不能替这些检查背书。

参数名与默认值可核对 [WeRSS 官方说明](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/ReadMe.md) 和上述生成器。不要把 README 展示的快速启动默认凭据直接用于公开服务。核对版本部分 RSS 路由的认证依赖被注释；应在受控入口进行鉴权，不能因存在一个认证函数就认定所有订阅入口已受保护。

## 在偷闲中启用

1. 管理员新增来源，选合适适配器：RSS/Atom 为 `rss`、RSSHub Atom 为 `rsshub`、WeRSS XML 为 `werss`、标准 JSON Feed 或授权 JSON 接口为 `authorized_api`。网站适配器不能启用订阅正文模式。
2. 默认 `feed_content_mode=link`，按链接采集。确认桥接输出确为完整正文后，管理员才选择 `feed_content_mode=authorized_fulltext`。
3. 对来源重新核验，填写 `verification_basis`：原始公众号或机构的身份依据、桥接服务运营方、内容使用授权范围和核验时间。仅一个可以打开的 RSS 地址不能证明是官方来源。未核验来源不会消费完整订阅正文；变更 URL、适配器、来源身份或正文模式后重新核验。
4. 私有订阅访问凭据放服务端 `CAMPUS_SOURCE_<来源 UUID 去连字符并大写>_TOKEN`，由服务端以 Bearer 头发送。需要网关兼容这种认证方式。公开网桥接必须使用 HTTPS；只有精确列入受控网关信任的内网 origin 才允许 HTTP。采集器在 DNS 请求前拒绝向普通 HTTP 地址发送 Authorization/Cookie/API key。不要把口令、Cookie 或 token 放到 URL、公共目录、前端配置或学生可见字段。
5. 在管理员采集状态中检查成功数、待补采数、失败和截断标记，再抽查原文依据。超过当前订阅窗口的历史文章不会仅凭 `limit=100` 自动覆盖；桥接需要自行保留足够窗口，或安排有界历史补采。

应用只在明确启用并已核验的来源上使用订阅正文。原始订阅响应存私有对象存储，条目正文由该次响应重放，保留内容哈希和抓取时间；积压记录只保存回执引用和正文哈希。证据说明会指出来自授权订阅，不能展示为“已直接核对原网页”。附件仍经过原有安全抓取检查，正文授权不会放开内网、云元数据、任意重定向或访问凭据转发。

## 解析行为和不确定性

| 输入 | 是否作为嵌入正文 |
|---|---|
| RSS `content:encoded`（正确 namespace） | 非空且未超限时提供给授权检查 |
| Atom `content type=html/text/xhtml` | 支持；纯文本先 HTML 转义，XHTML 必须是正确 namespace 的单个 div |
| Atom `content src=...`、二进制 content | 不跟随，不作为嵌入正文 |
| JSON 的显式 `content_html` 字符串 | 非空且未超限时提供给授权检查 |
| RSS `description`、Atom `summary`、JSON `summary` / 普通 `content` | 不作为完整正文 |

`embedded_complete` 只表示上游填了受支持的显式正文栏位，不能证明上游没有漏段、正文真实或授权存在。提取出的日期、对象、材料和更正仍须与保留的证据对应；需要附件才能确定的条件继续进入复核。上游若只输出摘要，换成 Atom 或 JSON 字段名也不能把摘要变成完整通知。

单次输入最多 8 MiB，最多解析 101 条；单条正文最多 128 KiB（UTF-8 字节计）。超限明确报错，不把截断的内容标作完整。XML 限深 64 层、限 20,000 个节点，拒绝 DOCTYPE、ENTITY、NUL 混淆和畸形结构。此解析器只做协议解析，不执行 HTML、JavaScript 或网络请求。它不是完整 Atom 标准验证器；供稿端应输出绝对文章及附件链接，避免依赖 `xml:base` 的相对链接语义。

Atom 文本与 HTML 的区分依据 [RFC 4287](https://www.rfc-editor.org/rfc/rfc4287)；JSON 正文栏位依据 [JSON Feed 1.1](https://www.jsonfeed.org/version/1.1/)；RSS 正文与摘要的区分参考 [RSS Best Practices Profile](https://www.rssboard.org/rss-profile)。

## 接入后需要证明的结果

先用合成 feed 验证链路：全文模式不请求微信文章，摘要缺少正文时明确失败；默认链接模式仍从原网页取正文；未核验源不能消费嵌入正文；凭据不进入快照公开字段；授权撤销、403 和超限有明确失败状态；重启后能按私有回执继续补采；正文、标题或发布时间更正生成可追溯的新版本。发布时间作为“明天”等相对日期的锚点，正文中的活动时间不会覆盖 feed 声明的发布时间。

再在一个已获得授权的真实公众号上抽查最近 10 条，并与桥接保存的正文和原始文章对照，记录缺失正文、附件、发布时间及更正的情况。至少观察一个更新周期，并测试授权失效与人工恢复。完成这些检查前，测试通过只说明本项目处理合成协议输入正确，不能宣传“任意公众号一键自动抓取”或“永久不漏通知”。

## 许可证与本次集成范围

RSSHub 当前固定提交使用 [AGPL-3.0](https://github.com/DIYgod/RSSHub/blob/1f5bc61c5409787cf564ddac21bca7e26f4fc862/LICENSE)，不是旧资料可能提到的 MIT。WeRSS 固定提交使用 [MIT](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/LICENSE)。本次独立编写标准 feed 解析器，没有复制、打包或修改这两个项目的源码，也没有把第三方服务作为已部署内容交付。若后续修改或分发它们，应分别依其许可证保留声明和履行相关义务；软件许可也不自动授予公众号文章的复制和公开展示权。
