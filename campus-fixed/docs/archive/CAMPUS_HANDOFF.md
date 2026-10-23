# 天大版实际实现基线与代理交接

## 1.1 来源与云部署交接

- 当前契约为 59 个操作。新增 `GET/POST /me/source-requests`、`GET /admin/source-requests`、`POST /admin/source-requests/{request_id}/resolve`；1.6 新增 `GET /events/{event_id}/changes`（公开事项每次采集的内容变化，DTO 为 `EventChangeSummary`/`EventChangeItem`/`ChangeSummary`）。DTO 见冻结契约。
- A0 拥有 `source_requests.py`，存储集合为 `source_requests/source_request_indexes/source_request_receipts/source_request_limits`。申请仅本人和运营可见，连接只接受已启用、核验、健康且成功采集的同类来源；不从学生提交网址直接发起网络请求。
- `Subscriptions.source_mode` 可选，`all` 忽略 source_ids，`selected` 包含空数组；遗漏字段沿用旧语义。收藏事项保留。事件类别设置独立。
- 云代理负责 `cloud_run.py/blob_store.py/deploy/cloudrun/requirements-cloud*`，仅在 GCS 模式加载 Google SDK；主依赖锁和本地文件模式不变。Web 服务禁用内置 worker，由有界 Job 共享 PostgreSQL 和私有桶。
- `CAMPUS_SOURCE_REFRESH_SECONDS` 控制共享采集间隔，范围 300..86400 秒，本地默认 21600，云部署使用 1800。Scheduler 每分钟运行队列不代表每分钟重复抓全部来源。
- 本轮执行分工、集成和兼容说明见 `CLOUD_SOURCE_RELEASE.md`。下面初版交接记录保留其历史上下文。

从原项目 desktop/attention-desk 的 React/Vite/FastAPI 入口复用工具链，另开安全的校园服务 server.campus_app:app；原本机服务 server.main:app 不公开挂载。对应原设计 A0/A1–A8，现在四个执行槽：主代理=A0/A4/A5/A6/A8；platform代理=A1；pipeline代理=A2/A3；web代理=A7。所有代理独立Git worktree。

实际目录：server/campus/platform.py、db.py 为A1；sources.py、content.py 为A2/A3；events.py、search.py、delivery.py、worker.py、campus_app.py 为主代理；src/campus/、campus.html 为A7。campus-contracts/、依赖与锁文件、vite配置、ops/由主代理维护。所有路径相对desktop/attention-desk。原服务文件不动。

## 共同运行时接口（冻结）

A1交付 server/campus/__init__.py、platform.py、db.py、schema.py；依赖FastAPI/httpx/jsonschema/SQLAlchemy/psycopg。其他模块不再造账号/队列。API前缀统一 /api/v1，工厂make_router(core)返回不带前缀的APIRouter，由主代理装配。

platform.py:
- Settings.from_env(): env、database_url、data_dir(Path)、secret、admin_emails、allowed_origins、smtp相关配置。开发SQLite，生产PostgreSQL；生产拒绝开发认证开关。
- Core(settings): core.settings、core.store、core.data_dir。可动态挂pipeline/events/search/delivery/handlers。Core为各服务共享依赖容器。
- require_user(request, roles=None, write=False) -> User dict；session Cookie查服务端，write=True检查CSRF，roles为允许角色字符串集合。失效统一401，角色403。
- make_router(core): auth challenges/sessions/logout、me/profile/subscriptions/catalog、admin/jobs/retry、health接口。
- APIError(status, code, message)异常；error_handlers(app)安装统一Error响应与request_id，含验证错误处理。
- MailGateway(settings).send_login_code(email, code) 与 send_delivery(email, subject, text, idempotency_key) -> receipt dict {status:sent|unknown|failed, provider_id:nullable string}。开发不真实发信；开发挑战可通过显式CAMPUS_DEV_AUTH=true响应头X-Dev-Login-Code展示一次性随机码，生产绝不泄漏。SMTP未配置不能宣称已发送。

schema.py:
- validate(schema_name, data) -> None，使用campus-contracts/domain-v1.schema.json的Draft202012Validator和FormatChecker；失败APIError(422,...)
- uid() -> UUID string，now_iso() -> UTC RFC3339。

db.py Store(settings.database_url):
- get(collection, key) -> dict|None；list(collection) -> list[dict]；revision(collection,key)->int（无记录0）
- put(collection,key,data,expected_revision=None) -> dict；delete(collection,key,expected_revision=None)->None；期望版本不匹配抛409。存储内部revision，不向DTO塞额外字段。读写JSON深拷贝。
- transaction() -> context manager，得到同一Store，支持模块业务写与emit/enqueue原子提交；嵌套复用同一事务；跨线程隔离。
- emit(topic, aggregate_id, aggregate_revision, **fields)->OutboxEvent；其他固定字段默认null/change_kind默认updated/schema_version1.0/occurred_at自动。实际业务ID及owner模块验证。
- enqueue(kind, payload, idempotency_key, run_at=None)->Job；payload只提供实际字段，补齐JobPayload固定字段。唯一幂等键，不重复建任务。
- jobs()->list[Job]；get_job(id)->Job|None；claim_job(worker_id,kinds=None,lease_seconds=120)->dict|None，返回{job:Job,payload:JobPayload}；complete_job(id)、fail_job(id,error_code,retry_delay=30)、retry_job(id)->Job。
- pending_outbox()->list[OutboxEvent]；mark_outbox_dispatched(id)；dispatcher必须在transaction里enqueue后标记。

Store允许的collection逻辑所有权：users/sessions/challenges/profiles/subscriptions/catalog=A1；sources/documents/snapshots/fetch_runs=A2；parsed/evidence/candidates/candidate_versions/reviews=A3；events/event_versions/event_snapshots/event_receipts/publication_audits/user_states/personal_items/personal_tombstones=A4；answer_cache/search_receipts=A5；preferences/digests/reminder_plans/deliveries/calendar_tokens/calendar_entries=A6；idempotency=A1基础（业务调用时按owner/operation/hash作用域）。数据库Record集合可按逻辑collection持久化，不能用共享JSON文件做多人状态；A1负责schema/migration。不要直接写别模块集合。

## 各服务交接

Pipeline(core) in content.py/sources.py：.handlers提供fetch_source/parse_document/extract_candidates函数，函数输入JobPayload；make_router(core)分别实现sources/admin/snapshot/evidence/parsed/reviews归属API。在事务中发source.snapshot.created/content.parsed/candidate.accepted，由主worker分派。候选revision历史保留；审核接受才通知事项归并。

Events(core)由主代理实现：get_public(event_id,version=None)、can_read_snapshot(snapshot_id)、current_snapshot_ids(event_id)、reconcile_candidate(candidate_id,revision)、selected_subjects(user_id)、resolve_subject(user_id,subject)、feed(user_id)；A2/A3用can_read_snapshot复核公开读取。reconcile只读candidate_versions固定accepted revision；新事项/版本发public_event.changed。个人状态和私人事项按owner，变化发outbox。

Content检索端口：Pipeline.parsed(snapshot_id)->ParsedDocument|None；.segments(snapshot_ids)->list[{snapshot_id,id,text,page}]；.register_evidence(snapshot_id,segment_id,quote)->Evidence，必须真实子串并去重。Search经此全文检索，不能只索引已有抽取quote。

HTTP公开DTO按campus-contracts，人工修正文和状态校验必须实现，不能假数据静默替代。前端只用统一api client和CSRF，生产失败显示失败。本地群文本只上传学生确认的PersonalItemDraft（摘录默认null）。

## 实际交付

每个代理完成后运行相关测试，提交自己分支，报告commit SHA、改动、依赖申请和未验证项。主代理按platform→pipeline→web串行cherry-pick，装配事件/搜索/提醒与worker，跑端到端和构建，交付启动脚本、Compose及完整源码包。外部真实授权和发信未配置时明确显示状态；不能把Mock/静态页面当应用完成。


## 集成扩展记录

- 增加四个管理端 GET（事项列表、快照、解析、原文件）；OpenAPI 共 51 操作，domain DTO 保持不变。
- 运行入口为 `server.campus_app:create_app`，不导入旧桌面系统 API。
- 版本快照映射使用 `event_snapshot_versions`；当前搜索仅使用当前已接受的正文与附件，历史读取仍受当前发布状态控制。
- 发送认领使用 `delivery_claims` 与 `delivery_families`；不确定的外部发送标记 unknown，不自动重发。
- 演示通过显式 demo 命令导入，runtime/demo 标记驱动界面提示。
