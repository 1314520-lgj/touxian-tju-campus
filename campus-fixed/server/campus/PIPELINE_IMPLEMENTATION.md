# A2/A3 实现交接（2026-10-08）

拥有文件：server/campus/sources.py、content.py、tests/test_pipeline.py、config/campus-sources.json。

## 端口与装配

应用创建 Core 后创建 Pipeline(core)，赋给 core.pipeline；调用 core.pipeline.sources.seed() 仅初始化官方来源身份，health=never_checked，不导入演示事项。core.handlers 更新 pipeline.handlers，worker 执行 fetch_source / parse_document / extract_candidates。分别包含 sources.make_router(core)、content.make_router(core)，统一挂 /api/v1。get_accepted_candidate(id,revision) 读取不可变 accepted 历史，并要求存在同一候选修订的 accept 人工审核记录。自动提取一律产生 pending / review_required=true，只有审核成功才发出 candidate.accepted；旧版自动接受标记不能授权发布。parsed / segments / register_evidence 提供 Search 所需正文与真实子串证据。

同一父正文快照的附件修改会生成同一 candidate_id 的下一 revision，snapshot_ids 只引用各附件最新快照，旧候选历史不改。A4 可通过 candidate_id 的已有归并记录定位事项；新正文快照按 document/source URL 归并。取消自动进人工审核，接受时必须提供 target_event_id；不会模糊匹配后自动取消别人事项。

已实现公共与管理来源列表、创建、修改、核验、异步采集、服务端变化提示；来源身份变更后核验回退 unverified。健康以实际抓取结果记录，不把网页身份核验当采集成功。JSON/RSS/Atom/网站适配器名 website/rss/rsshub/werss/authorized_api；authorized_api 返回 items[].url/title/published_at，凭据来自服务端环境变量。

公开 snapshot/parsed/evidence 需 A4 can_read_snapshot 每次复核发布；文件签名五分钟有效，撤回后即拒绝。管理 snapshot/parsed/file 按 A0 批准的新增契约，只允许 operator/admin，文件签名绑定 actor；每次读复核会话和角色，禁止学生和其他审核人复用链接。HTML 文件以下载附件返回，禁止在应用源执行。

## 配置

CAMPUS_CHANGE_TOKEN 为内部变更Webhook Bearer Token，至少32字符，不配置时401。变更只能触发已登记 source_id 的来源采集，不能任意指定URL。

CAMPUS_SOURCE_<去连字符并大写的源UUID>_TOKEN 为 RSS/API 网关凭据，作为 Authorization: Bearer 发到初始origin；不传给文章、不跨origin重定向、不进DTO/日志。创建/修改来源地址拒绝userinfo与常见秘密query参数。

CAMPUS_TRUSTED_GATEWAY_ORIGINS 由部署管理员设置逗号分隔的精确origin，例如 http://rsshub:1200,http://we-mp-rss:8001。只有该精确origin允许访问内网网关/非标准端口；后台编辑来源不会扩展此权限。网关重定向仍每跳复核，link-local/metadata 地址始终拒绝。不要加入198.18.0.0/15等范围例外。

## 安全与解析

默认只允许全球单播DNS结果，混合公网/内网DNS拒绝；连接固定到已验证IP，Host/TLS server_name保留原主机，不使用环境代理。每次重定向重新验证，HTTPS降级拒绝。8MiB未压缩响应、最多4次重定向、每次抓取15秒总预算（系统DNS解析受系统超时控制），压缩回复拒绝。每次来源最多8篇、每篇4个附件，附件没有递归抓取。大站定期扩展抓取需要配置或后续游标分页；当前第一屏增量供学生端使用。

HTML优先大学VSB/公众号正文选择器，回退Trafilatura；整页fallback进入partial+审核。PDF使用隔离子进程pypdf（20秒、最多100页、50万字符；Linux内存上限768MiB），缺文字页标记需OCR。DOCX读取受限Office ZIP XML；XLSX索引值标记partial要求人工核对。压缩包32MiB解压总上限、1000成员、200倍压缩比，禁止DTD/实体。图片/扫描PDF在未配置OCR时明确failed/partial，不把空内容伪装成解析成功。本版没有默认下载Docling/OCR模型。

日期复用原desktop/.agents/skills/attention-announcement-triage/scripts/triage_announcements.py的正则声明，复制至server/campus/date_patterns.py以便独立打包（保留MIT署名，AST仅读取声明，不运行原CLI）。仅对时间指令解析；相对时间依赖源发布时间，缺失则unknown。date-only保留日期，不补23:59。跨年/多个日期/部分正文/附件/不明义务进入审核。人工修正要保留真实evidence，标题、材料、动作、链接、截止时间和人群不能凭空填造。

## 验证与真实来源

运行：python -m unittest server.campus.tests.test_pipeline -v。使用真实A1 Core/SQLite/JSON Schema/登录+CSRF，22项通过；网络响应仅在安全单元测试中使用显式合成doubles，实际校站另行测试。

官方页面经浏览核验（2026-10-08）：
- 教务处通知：https://oaa.tju.edu.cn/bszy/tzgg.htm。当前列表包含公开公众号文章链接。
- 智算通知：https://cic.tju.edu.cn/xwzx/tzgg.htm，学院公告正文及PDF附件可在网页见到。
- 综合新闻：https://news.tju.edu.cn/xw/zhxw.htm。

本环境 SafeFetcher 实测三站均失败 unsafe_address，DNS返回 oaa=198.18.0.24/2001:2::5c，cic=.25/::5d，news=.26/::5e，属于代理fake-IP保留范围。已安全拒绝，没有伪造healthy/采集成功；真实互联网DNS部署后需要再测实际采集及学校布局。未登录公众号账号、未配置真实邮件、未配置OCR模型，RSSHub/WeRSS仅验证通用RSS/JSON适配，不声称已验证这两个外部服务真实账号。

依赖：FastAPI/SQLAlchemy/jsonschema等来自A1；beautifulsoup4已有；pypdf为PDF基础，trafilatura为可选正文回退。无需requests直连、不新增数据库迁移、不改root requirements。生产PostgreSQL并发集成和应用完整端到端由A0/A8继续验证。

## 事件身份：一个事件属于一条通知，而不是一个标题

`Events._strong_match()` 决定一条已接受候选是并入既有事件还是新建事件。原实现只要候选与某事件的快照**文档集合有交集**就立即判定同一事件。这个判据在常见情形下是对的（同一 URL 重新抓取必然同文档），但它会把**两条真正不同、只是标题相同的通知**并进同一个对外事件，而标题相同并不等于同一条通知——同名的「第N批」「补录」「更正后重新公示」都是独立通知。

收紧后的判据分两层，且都用 `_document_roots()` 先把快照折叠成**根文档**（附件快照经 `parent_snapshot_id` 上溯到正文），所以「一条通知 + 它的若干附件」仍然稳定地算作一个根文档：

1. **同源文档完全一致** → 直接同一事件（最常见的重抓场景，行为不变）。
2. **文档不同但标题相同** → 视为「可能合并但未证实」，仍然并为一个事件，但**强制 `status=under_review`**，绝不作为已确认的公开事实呈现；同时置 `_pending_merge_review` 标记，该标记在每次 `_strong_match()` 入口重置，不会泄漏到下一条候选。

回归测试见 `server/campus/tests/test_event_merge_identity.py`（5 项）：附件折叠、同 URL 重抓仍合并、同标题异文档不得静默定论、标记不跨候选泄漏、撤回后对外 404 且写入 `publication_audits`。

配套地，交互式 schema 浏览器（`/docs`、`/redoc`、`/openapi.json`）改为由 `CAMPUS_PUBLIC_API_DOCS` 控制，默认**仅在非生产环境**开启：这三个入口会逐个列出后台路由与请求体结构，公开只读实例不应对外广播。

真实数据实例（`tju-campus-online/`）首次上线后按上述判据审计：41 个事件带有多个快照，折叠后**全部为 1 个根文档**（正文 + 附件），**0 个事件存在两个互不相关的根文档**——即收紧前后在这一批真实数据上结论一致，收紧只是把此前「靠标题相同就静默合并」的隐含假设变成了显式且可复核的规则。
