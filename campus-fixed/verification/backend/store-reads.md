# 后端优化二：数据库读取不占写锁

修改 db.py：get/list/revision/jobs/get_job 的独立读取使用短读事务，SQLite 使用显式 BEGIN 保持一致快照，不再申请 BEGIN IMMEDIATE 写锁。已有写事务中的读取复用原连接，保留 PostgreSQL FOR UPDATE、异常回滚标记、内存 SQLite 单连接互斥和数据深拷贝。所有写入、任务领取和 outbox 分发事务保持原有语义。

新增 test_store_reads.py，12 个实例：5 类读取与未提交写入并发、跨 SELECT 快照一致性、文件/内存 SQLite 的读取自己的修改、整体回滚、捕获读取异常后仍回滚、返回值隔离与缺失值。修复前 6 failed / 6 passed，修复后 12 passed。完整回归 553 passed、8 skipped、0 failed、106 subtests passed，37.39 秒。

固定让另一写事务占锁 150 ms，每组 9 次：旧读取中位耗时 180.063 ms，新读取 0.315 ms；新读取只见提交前的版本，写者完成后才见新版本。复现命令：python -m verification.backend.benchmark_reads。此结果只说明合成 SQLite 竞争场景，不代表生产端到端延迟或吞吐量。

未验证：8 项 PostgreSQL 实机测试仍因无隔离测试 URL 跳过；真实生产负载未运行。未增加依赖或修改数据库结构。
