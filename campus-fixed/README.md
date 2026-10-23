# 偷闲·天大版

把天津大学分散的公开通知（教务、招生、讲座、竞赛、就业等）整理成「有依据的行动清单」：**与我有没有关系、什么时候截止、要准备什么材料、在哪里办**，每条都能点回原文核对。

- 后端：Python / FastAPI + SQLite（`server/campus/`）
- 前端：React / Vite（源码 `src/campus/`，已构建好的网页在 `dist/`，`web/` 是它的同内容副本）
- 当前版本 1.8.0，对外 API 契约 1.5.0（`campus-contracts/`）

## 来源与许可

本项目是基于 [sssssjw11/touxian](https://github.com/sssssjw11/touxian)（偷闲）的独立校园入口，感谢原作者。原项目的桌面聊天导入、系统/微信本机接口不接入本校园服务。

本仓库随附两份 MIT 许可证，请在再分发时一并保留：

- [`LICENSE-Touxian`](LICENSE-Touxian)：Touxian contributors（偷闲上游）
- [`LICENSE-Desktop`](LICENSE-Desktop)：x2y1eesss（桌面端部分）

## 本地快速演示

只需要 Python 3.12+（首次运行要联网从 PyPI 安装依赖；网页已构建好，不需要 Node.js）。

**Linux / macOS**

```bash
bash run-demo.sh            # 默认端口 8000
bash run-demo.sh 9000       # 指定端口
bash run-demo.sh --synthetic  # 改用带【演示】标记的合成数据
```

**Windows**（在资源管理器双击，或在命令提示符运行）

```bat
run-demo.bat
run-demo.bat 9000
run-demo.bat --synthetic
```

脚本会自动：创建 `.venv` → 安装 `requirements.txt` 的锁定依赖 → 以**演示模式**启动，只监听 `127.0.0.1`。然后浏览器打开 <http://127.0.0.1:8000>，`/healthz` 返回 `{"status":"ok"}` 即正常。Ctrl+C 停止。

### 为什么要用演示模式

正式登录靠邮箱验证码，需要配置 SMTP 才能发信；本地没有 SMTP，所以演示模式打开了「开发登录码」：在登录页输入任意邮箱，验证码会**直接显示在页面上**（不发邮件）。`demo-admin@tju.edu.cn` 是演示管理员，其他邮箱是普通学生身份。这个开关只在本机演示使用，**不要用于公网部署**（公网请用 `live` 模式并配置 SMTP，见 `deploy/`）。

演示模式下后台采集器默认**关闭**，不会访问任何外部网站；数据保存在 `.campus-sample/`（或 `--synthetic` 的 `.campus-demo/`），不会改动随包的 `data/`。删除这些目录即可重置。

## 数据说明（请务必阅读）

- 随包数据是 **2026-10-09 的历史公开样本**（15 个来源、75 个已发布事项、345 份原文快照），**不是实时数据**。其中很多事项的截止日期已经过去，界面会把它们折叠到「历史通知」并标注「已截止」。
- 样本里的 15 个信息来源**默认停用**，仓库没有替你开启任何自动抓取；要接入真实采集，需要运营者重新核验来源后再启用（见 `docs/archive/` 内的运营文档）。
- 样本来自学校网站的公开通知。**已对个人联系方式做脱敏**（`tools/mask_public_contacts.py`，计数报告见 `docs/MASKING_REPORT.json`）：手机号保留前 3 后 4 位（`138****5678`），邮箱保留首字符与域名（`a***@tju.edu.cn`），范围涵盖 `data/campus.sqlite3`（含全文索引）、`data/snapshots/` 的 HTML/文本原文快照、`deploy/snapshot/` 快照库与 `deploy/operator/staging/` 样例；原文快照因此已不再是官网页面的逐字副本，快照文件名/`content_hash` 已随之更新并通过 `RELEASE_MANIFEST.json` 校验。
  **仍未脱敏**：各部门办公座机（如 `022-xxxxxxxx`，属公开办公电话）、QQ 群号与公众号 ID（公开的群/账号标识，非个人联系方式）、联系人姓名；`.docx/.xlsx/.doc/.xls/.jpg` 二进制附件快照（17 个）经扫描无手机号/邮箱命中，未改动。再分发前请自行再评估。
- 通知、条件与办理要求一律以原发布方为准；办理前请点「查看原文」核对。

## 目录

| 路径 | 内容 |
|---|---|
| `班级使用说明.md` | 班干部/班级使用说明 |
| `run-demo.sh` / `run-demo.bat` / `demo_server.py` | 一键本地演示 |
| `server/`、`app.py`、`serve_cloud.py` | 后端与启动入口 |
| `src/campus/`、`dist/`、`web/` | 前端源码与构建产物 |
| `data/`、`config/` | 历史样本数据、来源配置 |
| `docs/archive/` | 历史文档归档（交接、更新日志、发布说明、验收记录等） |
| `AGENTS.md` | 给接手维护的 AI/工程师的入口 |

## 开发与测试

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest server/campus/tests -q          # 预期 8 个 PostgreSQL 用例被跳过
```

重建网页需要 Node.js 24 与 pnpm 10：`pnpm install --frozen-lockfile && pnpm build`，构建后请把 `dist/` 同步覆盖到 `web/`（有测试检查两者必须一致）。
