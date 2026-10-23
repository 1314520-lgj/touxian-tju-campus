# 部署与真实数据库验收记录

日期：2026-10-08。此记录覆盖本地隔离环境，没有 Google Cloud 项目、云端构建或上线结果。

## 已解决的问题

- Scheduler 每分钟启动一次、worker 可运行 240 秒，仅设置一次执行内 `parallelism=1` 会产生多个并行执行。新增专用 PostgreSQL 会话 advisory lock，整轮 drain 持有，失败者明确跳过；队列事务不能释放这把会话锁。
- 更新环境时已存在的 Scheduler 会继续触发旧配置。部署脚本先暂停调度，显式启用时才恢复；失败保持暂停。暂停不停止旧执行，升级到本版本前须先完成旧执行维护检查。
- 新增不写库的预检，核对配置材料及真实环境的 schema/队列/健康。默认只检查本地文件，显式参数才读取数据库或访问 HTTPS 服务。
- 构建上下文增加私钥、数据库、凭据 JSON 和本地部署参数排除。feed secret 参数校验提前至任何云操作之前。

## PostgreSQL 发布物与边界

已通过正常 TLS 验证读取 [PostgreSQL Windows 下载页](https://www.postgresql.org/download/windows/)，其 zip archive 链接指向 [EDB binaries 页](https://www.enterprisedb.com/download-postgresql-binaries)。当前 17.11 Windows x86-64 链接为 `https://sbp.enterprisedb.com/getfile.jsp?fileid=1260616`，302 到 [EDB 17.11-5 便携 ZIP](https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64-binaries.zip)；未跟随陌生主机或关闭证书验证。

实际下载 381,531,864 字节，22,033 条 ZIP 记录，无路径越界，全部 CRC 通过。`postgres.exe --version` 返回 `postgres (PostgreSQL) 17.11`。三个核心程序 postgres/initdb/pg_ctl 的 Authenticode 均为 **NotSigned**，未取得独立的官方发布哈希。下列 SHA256 仅记录此次下载的本地完整性，不能称为官方验签或独立真实性证明：

```text
80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f
```

二进制只解压在工作区，没有安装 Windows 服务或修改全局 PostgreSQL。测试生成随机密码、随机 loopback 端口与临时集群，仅监听 `127.0.0.1`，使用独立 `campus_operations_test` 数据库；每个测试再创建独立随机 schema。测试后停止服务器并删除临时集群。Windows PostgreSQL 会降低 Administrator 组权限，运行器只为当前用户在本次临时目录增加显式访问权，没有放宽其他目录。

## 实际结果

`test_postgres_local.py`：**18 passed，8 subtests passed**，4.30 秒测试时间（不含初始化）。其中 8 个测试直接连接真实 PostgreSQL；4 个执行锁 SQL 协议测试、6 个只读预检单元测试。

真实 PostgreSQL 验证内容：

1. 两个独立 Store 并发首次插入与更新，CAS 各只有一方成功，另一方得到版本冲突。
2. 相同幂等键同时入队返回同一任务；不同正文冲突，不产生第二条任务。
3. 记录、任务和 outbox 同事务回滚；被捕获的嵌套异常仍阻止外层提交。
4. 两个执行器通过 SKIP LOCKED 领取不同任务；租约到期被替代后，旧执行器不能确认新租约。
5. 两个 worker 并发 outbox fanout 只生成一份后续任务；新 runtime 仍能读取持久任务状态。
6. 已持执行锁时，另一 worker 输出 skipped 且不触碰 run_once；释放后可运行；drain 异常仍释放锁，锁连接与记录事务独立。
7. 直接终止独立的持锁 Python 进程，PostgreSQL 释放会话锁，下一执行可重新获取。
8. 对已初始化真实 schema 执行只读 preflight，表/版本/队列检查通过，原任务状态不变。

`test_deploy.ps1`：八种本地替身部署情形与 PowerShell 语法通过：默认/带开关 dry-run 不调用 gcloud、无效 feed Apply 在任何调用前拒绝、已有调度暂停更新恢复、仅私有更新保持暂停、缺失调度创建、构建失败保持暂停并停止后续部署、非私有桶在任何写操作前拒绝、调度查询失败立即停止且不误判成“没有调度”。查询使用官方 [gcloud scheduler jobs list](https://docs.cloud.google.com/sdk/gcloud/reference/scheduler/jobs/list) 的精确名称 filter，成功空列表才表示不存在。这里的 Apply 使用函数替身，未连接实际 Google API。

本工作树的全部后端回归：**176 passed，8 skipped，15 subtests passed**；跳过的 8 项在上述单独真实 PostgreSQL 运行中全部通过。仅有一项既有 Starlette/httpx 弃用警告，没有修改依赖锁。

`preflight.py` 默认六项本地检查全部通过、`network_requested=false`。镜像 digest 检查只核对格式，不能证明镜像此刻可拉取；冻结依赖格式检查不能替代镜像实际构建。

## 复现

从应用根目录使用已安装后端依赖的 Python：

```powershell
python deploy/cloudrun/preflight.py
& ./deploy/cloudrun/test_deploy.ps1
python deploy/cloudrun/test_postgres_local.py --postgres-bin C:/自己的已核验PostgreSQL/pgsql/bin
```

不传 `CAMPUS_TEST_POSTGRES_URL` 时，普通后端测试会明确跳过真实 PostgreSQL 8 项，不能把跳过算成通过。运行器自动设置指向临时集群的 URL；也可手动设置独立 loopback、库名以 `_test` 结尾的验收数据库。测试只创建/删除自己生成的随机 schema，拒绝远程或非测试库 URL。

## 仍需上线后验证

Cloud SQL 实例规格、连接预算、IAM/Secrets、私有 GCS、SMTP 真实收信、实际镜像构建、Scheduler 触发、学校网络/微信内浏览器对 run.app 的可达性、学校源站和授权公众号的采集、备份恢复均未在 Google Cloud 实测。本次没有访问学生真实数据或创建收费资源，便携 PostgreSQL ZIP 不进入交付包。
