# 公网实例更新部署手册（第 12 轮发布包 + 2026-10-16 数据快照）

本文档给「有平台环境或可达主机」的运营方：把**第 12 轮最新代码**（搜索命中携带
`ended`、「已截止」标签）与周期 1 真实数据快照部署到公网，替换当前跑第 10 轮旧代码的
实例（`https://ec728a4b88f943a7b3f2bc39b7d22d52.sg2.agentos-app.run`，核验时仍在
线、为旧代码）。

## 0. 部署前置事实

- 产品版本 **1.8.0**，公共契约 **1.5.0**（第 12 轮为向后兼容字段新增，不破坏）。
- 公网部署 = 单端口托管沙盒（agentos-app.run）或可达主机，运行 `serve_cloud.py`；
  平台注入 `PORT`，绑定 `0.0.0.0`。
- 本发布包**不含**活凭证/隐私集合：清单拒绝 `deploy/operator/session.json`、
  `.server-secret` 与 `-wal/-shm`；数据快照已在副本上擦除 7 个隐私集合（见
  `deploy/snapshot/README.md`）。
- 沙盒文件系统持久性由托管方决定；建议在部署平台侧保留数据目录（第 10 轮实测
  同目录再次发布会覆盖代码、数据目录保留——以平台实际行为为准，部署后立即核验）。

## 1. 解包与放置

```sh
# 1) 解包（ZIP 顶层为 campus-improved/）
unzip 偷闲天大版-<轮次>-发布包.zip -d /srv/campus
cd /srv/campus/campus-improved

# 2) 安装锁定依赖（Python 3.12+）
python -m pip install -r requirements.txt

# 3) 放置数据快照（deploy/snapshot/ 已随包）——部署数据目录可以是任意受控持久目录
mkdir -p /srv/campus-data
cp deploy/snapshot/campus-snapshot-2026-10-16.sqlite3 /srv/campus-data/campus.sqlite3
chmod 600 /srv/campus-data/campus.sqlite3
```

> 校验快照哈希：`sha256sum /srv/campus-data/campus.sqlite3` 应等于
> `6d8786e30112217e2cc62e367e16e6836e2b7dc569dee1d2c0cc816ae080c7c7`。

## 2. 启动（live 模式，快照只读，不假装更新）

```sh
export CAMPUS_CLOUD_MODE=live
export CAMPUS_DATA_DIR=/srv/campus-data
export CAMPUS_EMBEDDED_WORKER=false
export CAMPUS_SECRET="$(openssl rand -hex 32)"   # 显式持久密钥，重启不变
# PORT 由平台注入（托管沙盒）或显式设置（可达主机，如 8000）
python serve_cloud.py
```

要点：

- `live` 模式**强制关闭开发验证码**（即使环境继承了开发验证码变量）；
- `CAMPUS_EMBEDDED_WORKER=false`：内嵌采集 worker 关闭——快照不假装更新；
- 托管沙盒自动放行「来源恰为当前宿主」的写请求（同源语义），跨站伪造仍被拒；
- 若要在可达校园网的主机上做**真实采集**（不是本次快照部署），把
  `CAMPUS_EMBEDDED_WORKER` 置为 `true`，并按 `REAL_INGESTION.md` 验收——本次不启用。

## 3. 部署后逐项核验（对公网实例实测，非本地复述）

| # | 检查 | 期望 |
|---|---|---|
| 1 | `GET /api/v1/health/ready` | 200，`{"status":"ready","demo_data":false,...}` |
| 2 | `GET /` 与 `/assets/*` | 200（前端含第 12 轮构建，搜索页有「已截止」标签） |
| 3 | `GET /api/v1/events?limit=10` | 3 条周期 1 真实事件（四六级报名/AI+合成生物大赛/本科生招生章程），`entry_urls` 为真实官网 |
| 4 | `GET /api/v1/search?q=招生` | 命中且**每条 hit 含 `ended` 布尔字段**；搜索页对 `ended=true` 渲染「已截止」 |
| 5 | `GET /api/v1/search?q=报名` | 命中含 `ended`；对照快照中 14 个已截止命中（如「2026年伦敦大学学院暑期学分项目报名通知」）应 `ended=true` |
| 6 | `POST /api/v1/auth/challenges` | **无 `X-Dev-Login-Code` 响应头**；登录接口 503（live 模式强制关闭开发登录码） |
| 7 | 伪造跨站 Origin 的写请求 | 403 `ORIGIN_FORBIDDEN` |
| 8 | `GET /openapi.json` | 404（文档未暴露） |
| 9 | 数据目录 | 库文件仍在、可读；`campus.sqlite3` 无 `-wal/-shm` 随包泄漏 |

## 4. 更新记录

部署完成后请回填：新实例 URL、核验结果（上述 9 项）、部署时间、托管方/主机信息，
供 `VERIFICATION.json` 与 `AI_IMPROVEMENT_LOG.md` 记录（本仓库第 13 轮「部署准备」
只备好包与手册，**未**替你把实例发出去）。

## 5. 边界（如实）

- 快照数据截止 2026-10-13；强基计划候选待运营方下周期重新采集复核后并入下一份快照。
- 未配置真实 SMTP/推送；个人推送未绑定网关时界面提示不可用。
- 沙盒出网到不了 `*.tju.edu.cn`：托管沙盒上的实例无法自动采集，只能做只读快照；
  真实采集需可达校园网主机。
- demo 实例（`6923f20c…`，第 5 轮，合成数据）本轮核验时已返回 400（疑似下线/回收），
  不在本次更新范围。
