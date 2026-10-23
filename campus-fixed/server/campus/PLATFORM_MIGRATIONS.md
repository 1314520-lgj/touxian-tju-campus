# A1 平台存储、认证与迁移

平台入口必须由 server.campus_app 装配；不挂载原 server.main 的本机读取接口。运行时依赖 FastAPI、SQLAlchemy、jsonschema、psycopg。Core(settings) 创建持久化 Store 和 MailGateway，request.app.state.core 必须指向相同 Core。

## 初始化与版本

当前 schema version 为 1。首次启动创建 campus_records、campus_jobs、campus_outbox、campus_schema_versions 四表。JSON业务DTO与内部revision、队列租约分别存储；不向DTO混入元数据。SQLite通过BEGIN IMMEDIATE串行事务并启用WAL/30秒busy timeout；PostgreSQL通过事务内 advisory lock 串行启动DDL、SELECT FOR UPDATE锁记录、SKIP LOCKED抢占任务。超过version 1的数据库拒绝运行旧代码。后续真实结构变更必须先增加显式migration，不能仅改Table定义而依赖create_all。

部署前备份数据库与CAMPUS_DATA_DIR。生产用PostgreSQL（CAMPUS_DATABASE_URL=postgresql+psycopg://...），开发默认data_dir/campus.sqlite3。SQLite备份应停服务或使用sqlite backup API，不能只复制活动主文件而遗漏WAL；生产用pg_dump/pg_restore。当前未对已有原偷闲本机JSON数据做自动迁移：新校园服务独立存储，原本机工具保留。

## 事务、版本与重放

Store.transaction()复用同线程连接，内层失败标记整笔事务回滚，即使外层捕获错误也不能静默部分提交。业务put/emit/enqueue要在同一外层事务。put/delete的expected_revision=0表示记录不存在，修改提交读取到的内部revision；冲突抛APIError 409。JSON读写都深拷贝。

enqueue的全局幂等键需由owner/operation/subject revision及内容hash构造，不得含学生原文或邮箱。相同键同任务返回原Job，不同payload返回409。固定JobPayload字段由Store补齐；调用者通常只给source_id/snapshot_id/candidate_id等实际字段。

claim_job后complete_job/fail_job须在同一个Store实例和同一线程调用。当前租约token存在执行线程内；其他进程重抢后，旧执行器提交被409 JOB_LEASE_LOST拒绝。返回DTO不含内部token。job处理者仍必须实现业务幂等，因为外部副作用与数据库提交不能保证exactly-once。长时间附件任务应配置较长claim lease（上限3600秒）或后续显式扩展heartbeat协议。失败自动最多6次，retry_delay=None立即终止。人工只能重放failed，累计最多3次；取消/成功/运行中不能人工重放。

dispatcher须在Store.transaction()内读pending_outbox、enqueue及mark_outbox_dispatched，避免事件已标记但下游任务未持久化。多dispatcher在PostgreSQL通过SKIP LOCKED避免同时抢同一事件。删除已有业务对象的owner保留必要tombstone供重放判断。

## 认证与配置

CAMPUS_ENV仅允许development/dev/test/testing/production/prod，未知值启动失败。生产要求CAMPUS_SECRET至少32字符、PostgreSQL、HTTPS精确allowed_origins、禁用CAMPUS_DEV_AUTH。Cookie为touxian_session（HttpOnly、SameSite=Lax，生产Secure），服务端仅保存HMAC后的随机session token；7天到期。所有账号写请求要求X-CSRF-Token，附带Origin还须在CAMPUS_ALLOWED_ORIGINS白名单。管理员由CAMPUS_ADMIN_EMAILS明确邮箱列表指定；移除列表立即失去admin权限。没有学籍认证或假冒天津大学官方账号声明。

验证码随机6位，10分钟到期，只保存基于server secret的HMAC；使用后不可重放、最多5次猜测。按邮箱30秒冷却、6次/小时及IP哈希30次/小时限流。显式CAMPUS_DEV_AUTH=true时仅在非生产挑战响应X-Dev-Login-Code头展示一次；跨源前端须通过CORS expose_headers暴露该头。生产绝不返回验证码。默认开发secret通过写完后的hard-link原子发布到data_dir/.server-secret，app与worker复用同一个文件；此目录不能放进源码交付或公开静态目录。

邮箱身份仅验证邮箱控制权。邮箱校验为普通Email；用户是否天津大学学生不能仅从任意邮箱推断。普通账号默认student，配置管理员为admin；平台没有给普通用户提升角色的API。

开发环境绝不真实发信。未配置SMTP或发送失败时登录接口503 LOGIN_DELIVERY_UNAVAILABLE，不能宣称邮件已发送。生产SMTP需CAMPUS_SMTP_HOST/PORT/FROM，按需USERNAME/PASSWORD；默认STARTTLS=true，或SSL=true。登录验证码通过共享MailGateway发送，简报由A6使用send_delivery；SMTP接受返回sent，DATA阶段超时返回unknown，不能因此盲目立即重发并宣称成功。测试只Mock SMTP，无真实邮箱投递测试。

## 天津大学字典

GET /catalog提供天津大学官网学院（部）页的36条学院/研究院名称快照，2026-10-08核对。来源：[天津大学学院（部）](https://www.tju.edu.cn/xy_b_.htm)。稳定应用ID为computer/software/mechanical等；智能与计算学部细分为computer、cybersecurity、artificial-intelligence、software，抽取器不能把tju-cic等样例ID直接写入真实用户条件。机构调整后由A1更新目录revision与名称；用户major仍为自填文本。目录是学校配置，不是模拟学生数据，平台默认不seed来源、通知、用户或管理员。

## 验证边界

tests/test_platform.py运行真实临时SQLite与本地FastAPI TestClient。覆盖账号两用户隔离、OTP并发/重放/失败限制、CSRF/Origin/角色、乐观版本冲突、原子回滚、租约过期重放与旧worker fencing、有限重试、幂等队列/outbox、生产安全配置及SMTP歧义状态。没有实际PostgreSQL、真实SMTP、反向代理HTTPS、学校身份系统或公众号授权实测；主代理需在目标部署环境补这些集成验证。
