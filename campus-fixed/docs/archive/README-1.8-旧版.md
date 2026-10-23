# 偷闲·天大版

当前产品版本 **1.8.0**，公共契约 **1.5.0**。本版在 1.7.0 可靠性强化版上修复中文相对时间的正确性缺陷（`12月底` 曾按发布月份算成 10 月、`下个月5号` 无结果、`开学后两周内` 曾被当成发布日 +14 天），恢复公开样本的复核决策链（保留 103 条决定与理由、移除 actor），清除交付物中作者机器的绝对路径，并把前端 `dist/`、`web/` 双副本的一致性变成可失败的检查。升级与验证边界见 [UPGRADE_1_8.md](UPGRADE_1_8.md)，1.7.0 的变化见 [UPGRADE_1_7.md](UPGRADE_1_7.md)。

中文检索使用本地 FTS5 缩小候选范围和近义词扩展，最终结果沿用既有打分与排序；当前实现不以 BM25 分数排序。变化摘要与无障碍自动化的历史说明见 [CHANGE_HISTORY_SEARCH_A11Y_RELEASE.md](CHANGE_HISTORY_SEARCH_A11Y_RELEASE.md)，1.5 的旧推送方案已被本版替代，见 [PUSH_AND_CHANGE_RELEASE.md](PUSH_AND_CHANGE_RELEASE.md)。页面设计记录见 [UI_POLISH_RELEASE.md](UI_POLISH_RELEASE.md)，授权供稿与正文方法见 [WEB_RESEARCH_RELEASE.md](WEB_RESEARCH_RELEASE.md)，公众号接入见 [WECHAT_FEED_GUIDE.md](WECHAT_FEED_GUIDE.md)，部署材料见 [CLOUD_SOURCE_RELEASE.md](CLOUD_SOURCE_RELEASE.md)。

面向天津大学学生的信息工作台：把分散通知整理成“是否与我有关、什么时候、准备什么、在哪里办”，减少重复查看通知和查找入口的时间。

这是基于 [sssssjw11/touxian](https://github.com/sssssjw11/touxian) 的独立校园入口。原项目的桌面聊天导入、系统及微信本机接口不接入校园 HTTP 服务。

## 先运行演示

Windows，安装 Python 3.12+ 后，在本目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-campus.ps1 -Demo
```

打开 http://127.0.0.1:8000 。发行包包含构建好的网页；首次需要联网安装锁定的 Python 依赖。仅重建网页时需要 Node.js 24。

- 演示事项都带“【演示】”，不能用于真实办理。
- 使用普通邮箱登录是学生身份；`demo-admin@tju.edu.cn` 是本地演示管理员。
- 本机开发模式直接显示一次性验证码，不发送真实邮件；仅绑定本机端口。
- 演示数据独立保存在 `.campus-demo`，普通运行使用 `.campus-data`。不要把演示数据库用于上线。
- 关闭启动窗口或按 Ctrl+C 停止服务；再次运行会保留账号、待办和偏好。

Linux/macOS 或手动运行：在单独终端执行以下隔离配置，避免继承运营数据库、存储、搜索或推送设置。

```sh
python -m venv .venv
# 激活虚拟环境后
python -m pip install -r requirements.txt
export CAMPUS_ENV=development
export CAMPUS_DATABASE_URL=
export CAMPUS_MEILI_URL=
export CAMPUS_MEILI_KEY=
export CAMPUS_PUSH_USER_ENDPOINTS=
export CAMPUS_PUSH_ENDPOINTS=
export CAMPUS_BLOB_STORE=local
export CAMPUS_GCS_BUCKET=
export CAMPUS_PUBLIC_ORIGIN=
export CAMPUS_SECRET=
export CAMPUS_SMTP_HOST=
export CAMPUS_SMTP_FROM=
export CAMPUS_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
export CAMPUS_DEV_AUTH=true
export CAMPUS_ADMIN_EMAILS=demo-admin@tju.edu.cn
export CAMPUS_DATA_DIR="$PWD/.campus-demo"
python -m server.campus_app demo
python -m server.campus_app serve --port 8000
```

## 已实现的学生流程

1. 填写学院、学段、年级、校区；在“信息来源”按名称、网址和类别找来源，关注全部或只关注自己选择的来源，包含一个也不选。
2. 查看“今天”和机会列表，按适用身份、期限与办理状态组织通知。对象或时间不明确时保留“待核对”。
3. 搜索完整解析正文，打开引用和不可变原文；问答返回期限、材料、入口及证据，不根据模型猜测补全要求。
4. 收藏公开事项并跟踪已读、待办、完成状态。改期或材料变化会提示重新核对，不覆盖个人完成状态。
5. 粘贴群通知或上传截图，在浏览器本地整理和 OCR，确认字段后创建私人事项。默认不上传整段原文或图片；只有显式勾选时同步摘录。
6. 接收站内简报和截止、变更提醒，配置免打扰时间；订阅个人 ICS 日历，可撤销链接。邮件需真实 SMTP，个人推送需维护者为本人配置独享的安全网关；未配置时界面提示不可用。
7. 未找到需要的网站或公众号时，提交公开网站地址、公众号名称或公开文章链接；查看处理进度，可选择接通后自动关注。申请仅本人和运营可见，运营须核验并完成一次成功采集后接入。

截图 OCR 首次使用会下载 Tesseract 的语言资源；不含离线模型，加载失败会真实显示失败。公众号消息通过经过授权的外部 RSSHub/WeRSS feed 接入，配置方法见 [ADAPTERS](deploy/campus/ADAPTERS.md)。此版本没有绕过微信授权，也没有伪造微信消息推送成功。

## 管理与采集

预置 15 个官方栏目身份，覆盖教务、研究生、就业、国际交流及部分学院，具体目录见 [SOURCE_COVERAGE.md](SOURCE_COVERAGE.md)。注册来源不等于成功采集，界面显示最后检查时间和真实健康状态。管理员可以新建来源、人工核验身份、触发采集、审核候选、查看正文附件和失败任务、撤回或恢复事项。

采集支持网站、RSS、RSSHub、WeRSS、授权接口；保存不可变 HTML/PDF/Office 快照、段落及页码证据。公开网页不直接作为可信指令执行。模糊日期、冲突日期、解析不完整、未核验来源等情况进入人工核对。

订阅默认按链接抓取；管理员可对已核验且有授权依据的来源开启“使用已授权订阅正文”，读取明确的 RSS content:encoded、Atom content 或 JSON content_html。摘要不充当全文，学生引用标明供稿方式。原始 feed 私有留存，积压队列只存回执引用，服务重启后可继续补采；附件继续独立安全抓取。正文清理规则及验证边界见 [BODY_QUALITY.md](BODY_QUALITY.md)。

随包公开样本来自用户提供的截至 **2026-10-09** 的历史公开通知，本轮没有重新采集学校网站。旧包记录有 **161 个附件失败**，不能将正文样本视为附件齐全或最新完整通知库。详情会提示当前依据的附件验证码、访问受限或解析不完整；请回到原网页核对。维护者取得授权后补充正文或附件时，补充内容须重新进入审核，不能直接替代已经核对的公开依据。

真实校园采集需在目标部署环境验证。采集器会拒绝私网、元数据地址或混合 DNS；注册来源和读取历史样本均不等于本轮成功采集。

默认离线的只读网络验收器可显式启用网络后抽样栏目、正文与附件，见 [NETWORK_ACCEPTANCE.md](deploy/cloudrun/NETWORK_ACCEPTANCE.md)。Google Cloud 的固定 run.app 网址不等于固定采集出口，也不能证明中国校园网络可达。

## 架构与职责

```mermaid
flowchart LR
  S[校园网站 / 授权公众号 feed] --> P[采集与不可变快照]
  P --> X[正文附件解析 / 证据]
  X --> R[候选与人工核对]
  R --> E[公开事项 / 版本归并]
  E --> F[学生画像与个人工作台]
  E --> Q[全文搜索 / 带依据问答]
  L[浏览器本地截图与文本整理] --> C[学生确认字段]
  C --> I[仅本人可见的事项]
  F --> D[提醒 / 简报 / ICS]
  I --> D
  O[数据库事务 Outbox / 持久化任务] --> P
  O --> X
  O --> E
  O --> Q
  O --> D
```

| 责任 | 文件 | 可独立修改的边界 |
|---|---|---|
| A0 总集成 | `server/campus_app.py`、构建、契约、发布包 | 装配、全流程测试、版本与依赖 |
| A1 账号与存储 | `server/campus/platform.py`、`db.py`、`schema.py` | 邮箱登录、角色、CSRF、画像、CAS、任务与 Outbox |
| A2/A3 信息与证据 | `server/campus/sources.py`、`content.py`、`zh_time.py` | 来源、抓取、解析、相对时间补充、引用与审核 |
| A4 事项 | `server/campus/events.py`、`change_detect.py` | 归并、版本、公开状态、采集变化摘要与私人事项 |
| A5 搜索 | `server/campus/search.py`、`zh_index.py` | FTS5 候选缩小或 Meilisearch、近义词扩展、既有排序、历史查询与依据问答 |
| A6 投递 | `server/campus/delivery.py`、`channels.py`、`worker.py` | 个人网关绑定、计划、重试、简报、发送认领与日历 |
| A7 Web | `src/campus/` | 学生与管理界面、本地 OCR |

多 AI 协作先冻结 [DTO](campus-contracts/domain-v1.schema.json) 和 [1.5 公共 API 契约](campus-contracts/openapi.v1.json)，在各自分支按拥有文件实现，提交后由 A0 顺序合并。共享结构的变化由 A0 统一落地；业务模块通过 Core、事务 Outbox 和明确 handler 接口连接。详细接口交接见 [CAMPUS_HANDOFF](CAMPUS_HANDOFF.md)，目标蓝图与运行版差异见 [IMPLEMENTED](IMPLEMENTED.md)。

## 个人推送配置

推送目标由维护者保存在服务器环境变量 `CAMPUS_PUSH_USER_ENDPOINTS` 中，格式为用户 UUID 到 HTTPS 网关地址列表的 JSON。UUID 使用登录后 `/api/v1/me` 返回的 `user.id`，每个目标须经用户确认实际收件人仅为本人。下面的 UUID 与域名均为占位示例，不能直接用于投递：

```json
{"11111111-1111-4111-8111-111111111111":["https://push.example.invalid/users/student-a"]}
```

网关须接受 JSON `title`、`body` 和 `Idempotency-Key`；本版不承诺任意钉钉、飞书或企业微信 webhook 可以直接使用。每人最多 4 个公网 HTTPS 443 端点；相同规范化地址被不同用户共享时，双方推送都会被拒绝。同一人的等价重复目标只发送一次。地址或密钥不返回浏览器，旧的 `CAMPUS_PUSH_ENDPOINTS` 全局广播配置被忽略；无需安装 Apprise 或额外推送依赖。API 与 worker 须读取同一份映射，变更配置后重启相关实例。

`GET /api/v1/me/push-status` 需要登录，只返回本人 `available` 与 `reason`（`ready`、`unconfigured`、`invalid_configuration`、`shared_destination`）。`ready` 表示绑定配置可用，不表示已经完成真实送达验收。未安全绑定时，界面禁用或提示个人推送不可用，`PUT /api/v1/me/preferences` 选择 `push` 会返回 **409 `PUSH_NOT_READY`**；已排队但后来失去绑定的任务会失败，不会退回全局广播。

每次发送前检查全部目标的公网 DNS 地址，连接固定到已核验 IP 并保留 TLS 主机名校验，拒绝私网、元数据、混合 DNS 和重定向。消息最多 16 KiB，传输有超时限制；系统 DNS 解析耗时仍由操作系统管理。发送前能确定未外发的错误记为 `failed`，发送后结果不明或只有部分目标接受记为 `unknown`，不盲目重发。本轮验证使用模拟连接，没有真实外发。

## 运行和发布

本机默认 SQLite，嵌入单个后台 worker；搜索默认使用同库的 FTS5 本地候选索引（一元 + 二元切分），索引缺失或凭据过期时回退为全量段落扫描。索引负责缩小候选，匹配与排序仍由既有检索逻辑完成。配置 `CAMPUS_MEILI_URL/KEY` 后可使用 Meilisearch，索引落后会显示状态。生产要求 PostgreSQL、显式 HTTPS origin、持久化密钥、真实 SMTP 和独立 worker。配置示例见 [生产环境示例](deploy/campus/.env.example)，备份步骤见 [DEPLOY](deploy/campus/DEPLOY.md)。

公开启动入口 `app.py` 保留 SQLite 的 WAL/SHM 与持久密钥，数据目录不可写或数据库损坏时明确停止，不自动复制到临时目录继续提供陈旧数据。完整 `dist/` 可直接读取，只有缺少可用前端时才从完整 `web/` 构建原子副本。历史链接及公开样本说明见 [README-ONLINE.md](README-ONLINE.md)，本轮没有部署或核验该历史链接。

Google Cloud 方案使用 Cloud Run 服务的固定 `*.run.app` 地址，Cloud SQL 保存数据库，私有 Cloud Storage 保存不可变原文，独立 Cloud Run Job 处理队列和定时采集。部署脚本默认只预览命令；准备项目后按 [Cloud Run 部署指南](deploy/cloudrun/DEPLOY.md) 填入资源和密钥版本再执行。当前尚未创建云项目或发布公网服务。

没有配置外部大模型也能完成基于证据的字段问答，日常运行不需要持续消耗模型 token。此处不是已经上线的学校官方服务；PostgreSQL、Meilisearch、真实邮件、真实公众号授权和校园采集需在目标环境联调。

## 验证与边界

```sh
python -m pytest server/campus/tests -q
node --test src/campus/tests/boundaries.test.mjs src/campus/tests/feedView.test.mjs src/campus/tests/sourceSelection.test.mjs
npm run build
```

无障碍扫描需要先启动合成演示服务，再运行 `CAMPUS_E2E_URL=http://127.0.0.1:8015 node src/campus/tests/a11y.e2e.mjs`；引擎为 `src/campus/tests/vendor/` 中的 axe-core，运行时不联网。

测试覆盖真实 SQLite、HTTP、审核归并、用户隔离、改期、撤回、删除、任务重试、个人推送隔离与投递竞争、公开采纳快照的完整变化历史、附件质量提示及中文检索候选等价性；网络与 SMTP 使用明确标记的合成 fixture。具体测试、浏览器与无障碍结果 **以最终验收报告为准**，见 [ACCEPTANCE](ACCEPTANCE.md)。

私人事项不进入公共搜索。原文只有日期时保持全天日期，不补造 23:59。当前查询不混入已过期版本；历史查询显式标注版本。变化记录汇总历次已公开版本采纳的公开快照；未公开或未采纳的中间稿不会出现。撤回会同时阻断公开事项、变化历史、搜索、证据与原文件入口。发送结果不确定时标记 unknown，避免盲目重发。

## 来源与许可

保留上游 MIT 许可证与署名。实现参考 RSSHub、WeRSS、changedetection.io、Docling、Meilisearch 的职责边界；未把这些完整项目复制拼接到服务。使用的 Python/Node 依赖在锁文件中固定版本；外部公众号 feed 由其运营者提供与维护。详细项目选型和分阶段任务另见配套架构与 AI 协作包。
