# 运行版实施记录

配套“架构与 AI 协作包”是完整目标蓝图；当前可运行交付与它共享 domain DTO 和 API，以下记录实际实现选择，避免把规划功能当成已经接通的外部服务。

| 蓝图 | 当前交付 |
|---|---|
| React/Vite/TypeScript | 沿用原仓 React/Vite/JavaScript，运行时使用冻结 JSON Schema 校验 API；未做全仓 TypeScript 迁移。 |
| PostgreSQL | 生产配置要求 PostgreSQL；本机演示 SQLite。真实数据库并发测试当前使用 SQLite，PG 需目标环境联调。 |
| Meilisearch | 已有真实 HTTP 索引适配；未配置时检索已接受的完整段落。已配置却不可用时返回失败，不伪造索引成功。 |
| Trafilatura/Docling | 正文可用 Trafilatura；PDF 文本使用隔离 pypdf，Office 使用有界解析。Docling/OCR 是可选服务端适配，缺失时保留失败/不完整状态。浏览器截图 OCR 已实际验收。 |
| 对象/本地文件存储 | 1.1 增加私有 Google Cloud Storage SDK 适配与有界哈希核对；本地仍用持久化快照目录。GCS 协议替身测试通过，真实云桶尚需目标环境验收；未交付 S3 实现。 |
| RSSHub/WeRSS | 实现外部 HTTP feed 适配、授权环境变量和健康状态；不附带平台登录账号或公众号凭据。 |
| changedetection | 已提供受服务令牌保护的变更提示接口；外部 watcher 按需配置，没有捆绑运行。 |
| AI 问答 | 采用确定性的证据字段提取与引用返回，不需要外部大模型密钥，避免虚构办理要求。 |
| 原始 47 项 API | 管理审核补充 4 个只读接口；1.1 再增加 4 个学生来源申请/运营处理接口，共 56 项，均有真实 HTTP 成功响应契约验证。Subscriptions 增加可选 source_mode，保留旧客户端语义。 |
| 学生自主选择来源 | 可搜索目录、按类筛选、全部/所选/零选订阅，提出未收录网站和公众号申请。运营核验、成功采集后连接，按本人同意自动关注；学生提交网址不会直接触发采集。 |
| Google Cloud 固定网址 | 提供 Cloud Run + Cloud SQL + 私有 GCS + 有界 Job/Scheduler 部署材料，脚本默认 dry-run。尚无云项目，未执行云构建、创建资源或生成真实公网网址。 |

分工仍按 A0 总集成、A1 平台、A2/A3 采集证据、A4 事项、A5 搜索、A6 投递、A7 界面拆分。交付包含模块、持久化任务、契约、回归测试和部署材料。真实校园通知的抽取准确率、授权续期、邮件送达与生产依赖需要在实际环境中测量。
