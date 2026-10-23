# 公网部署数据快照（2026-10-16）

本目录存放「偷闲·天大版」第 12 轮发布包随附的**干净只读数据快照**，供公网实例
（agentos 托管沙盒或可达主机）以 `live` 模式部署时作为数据目录使用。

## 文件

| 文件 | 说明 |
|---|---|
| `campus-snapshot-2026-10-16.sqlite3` | 干净快照库（1,060,864 字节（2026-10 已做联系方式脱敏，见 docs/MASKING_REPORT.json；原始 1,175,552 字节版本不再随包），sha256 `6d8786e30112217e2cc62e367e16e6836e2b7dc569dee1d2c0cc816ae080c7c7`） |
| `README.md` | 本说明 |

## 来源与制作（可复核）

- 来源：`data-operator/campus.sqlite3`（周期 1 运营实例库，活库）。
- 方式：`sqlite3.Connection.backup()` 一致性备份（自动并入 WAL 内容）→ 在**备份副本**上
  擦除 7 个隐私集合 → `PRAGMA integrity_check` = ok。**活库未被改动**。
- 擦除的隐私集合（共 35 行，副本内删除）：`sessions`(6)、`challenges`(12)、`users`(2)、
  `profiles`(2)、`subscriptions`(2)、`digests`(2)、`idempotency`(9)。
  这些集合含登录会话 CSRF、登录码挑战、邮箱、画像、订阅、个人摘要与限流键，
  **不得**进入任何公开部署。
- 保留的业务数据：`events` 3 条（周期 1 已发布：四六级报名通知、AI+合成生物大赛、
  2026 本科生招生章程）、`candidates` 4（3 采纳 1 拒绝）、`reviews` 4、`sources` 21、
  `evidence` 85、`snapshots`/`event_snapshots`/`event_versions`、`campus_jobs` 44、
  `campus_outbox` 14、`campus_segment_fts` 142（搜索索引）等，共 224 条记录。

## 部署用法（要点）

```sh
# 解包发布 ZIP 后，把本快照放入部署数据目录并命名 campus.sqlite3：
CAMPUS_CLOUD_MODE=live \
CAMPUS_DATA_DIR=<部署数据目录> \
CAMPUS_EMBEDDED_WORKER=false \
PORT=<平台注入端口> \
CAMPUS_SECRET=<显式持久密钥> \
python serve_cloud.py
```

- `live` 模式强制关闭开发验证码（登录接口 503，不泄露验证码）。
- `CAMPUS_EMBEDDED_WORKER=false`：快照不假装更新；要自动采集需一台能连
  `*.tju.edu.cn` 的主机（本快照数据截止 2026-10-13）。
- 完整逐步手册见 `DEPLOY.md`（同目录）。

## 边界（如实）

- 数据截止 2026-10-13（周期 1）；第 11/12 轮没有新运营周期，强基计划候选待运营方
  下周期重新采集复核后并入下一份快照。
- 沙盒文件系统是否跨重启/重新发布保留由托管方决定，本快照制作时未核验。
- 快照内的原文正文、证据、事件均为周期 1 运营方中继取证的真实官网/公众号内容；
  不再重新证明其正确性，以 `deploy/operator/` 周期证据为准。
