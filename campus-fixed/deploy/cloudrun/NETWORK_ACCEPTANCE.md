# 真实网络与来源采集验收

固定 `run.app` 网址解决入口地址稳定性；采集能否成功、天津学生能否访问，必须分别验证。本页的方法与实现于 2026-10-08 查阅 Google 官方文档和 GitHub 项目后整理。

## 已实现的只读验收器

`probe_sources.py` 复用产品 `SafeFetcher`、`feed_items`、`content_kind`，正文复用产品的纯函数 `html_body_segments`。不构造 `Settings` / `Core`，不连接或初始化数据库，不写来源健康、快照、任务、候选。正文策略尚未合入的旧版本会返回 `html_body_policy_unavailable`，不能冒充解析成功。

从应用目录运行：

```powershell
# 默认只核对随包官方目录，不解析 DNS，不请求网页。
python deploy/cloudrun/probe_sources.py

# 显式只读网络抽样：15 个栏目，每个最多 1 篇正文、每篇最多 1 个附件。
python deploy/cloudrun/probe_sources.py --network

# 单栏目复测；只接受官方目录已有 UUID，不接受任意 URL。
python deploy/cloudrun/probe_sources.py --network --source c4e00274-5b08-5d26-9a6d-19aed0d71571
```

输出 JSON 可重定向到自用报告文件。默认上限为 45 个 URL 请求，另有每个请求最多 4 次重定向；逐次间隔 0.25 秒。列表只识别当前页前 100 条，超出时输出 `listing_window_truncated=true`，不翻页、不证明历史完整性。`--articles` 允许 0–3，`--attachments` 允许 0–2；0 篇正文只得到 `listing_only`，不能作为正文验收通过。

保留产品每次请求 8 MiB 上限、15 秒 HTTP 截止时间、逐跳 DNS 校验与 IP 固定连接、HTTPS 主机名和证书校验、HTTPS 降级拒绝。系统 `getaddrinfo` 的耗时仍由操作系统管理，不在 HTTP 截止时间内；如果环境 DNS 卡住，运维应停止该次验收并检查解析器。没有自动重试、代理环境接管、备用 DNS、硬编码 IP 或关闭证书验证。验收器明确清空私网网关信任列表，不读取 `CAMPUS_SOURCE_*_TOKEN`，不检验授权 feed。

报告只含来源 UUID/公开栏目标识/官方主机、URL SHA-256、阶段、数量、解析状态、固定错误码和建议。不会输出网页正文、标题、完整文章/附件 URL、query、请求头、原始异常、IP、学生内容或秘密。收到 HTTP 200 仍会识别密码框、简短验证码/拦截页，并使用共享正文策略检查结构与空正文。已知拦截页的附件不会继续请求。

| 来源状态 | 含义 |
|---|---|
| `not_checked` | 本地预览，未接触网络 |
| `failed` | 列表未通过网络或结构检查 |
| `listing_only` | 只检查列表，正文未抽样 |
| `review_required` | 发现正文/附件/结构问题，或解析仍未完成 |
| `sample_passed` | 本次有限正文样本通过共享策略；不证明全部通知、持续健康或字段准确率 |

PDF、DOCX、XLSX 仅下载和识别类型，返回 `parser_not_run`；图片返回 `ocr_not_configured`；旧 Office 二进制返回 `unsupported_legacy_office_requires_review`。这能区分网络成功与证据可用，不能替代产品的隔离解析和人工审核。命令退出码：预览成功为 0；网络模式只有所有来源 `sample_passed` 才为 0；发现问题为 1；目录/参数无效为 2。

## 本机实测证据

2026-10-08（America/Port-au-Prince）显式执行了全部 15 个官方栏目的网络验收。结果为 **15/15 在列表请求前被 `unsafe_address` 拦截**。每个来源观测到 1 个 DNS 地址、0 个公共地址、1 个落在 `198.18.0.0/15` 的地址；没有请求正文或附件。这个范围与常见 fake-IP 模式相符，表明本机解析环境与产品的公共网络采集策略不兼容，不能据此断言学校网站宕机，也不能声称已完成真实采集。

原始计数报告保存在本次工作树 `.campus-network-probe.json`（运行证据，不进入生产镜像）。本地预览显示 15 个 `not_checked`。15 项专门测试通过，覆盖默认不访问网络、假 IP/混合公共私网解析、重定向重新校验、环境信任不生效、凭据不读取、HTTP 200 登录页、正文/附件有限抽样、列表截断和输出脱敏；另有 6 项原有部署预检回归通过。这些是合成安全/行为测试，不能替代学校实采。

## DNS 问题如何处理

先让网络管理员确认是否使用 Mihomo/同类工具的 fake-IP 模式。其官方文档说明，`fake-ip-filter` 在 blacklist 模式中可让匹配域名不返回 fake-IP。可以在**现有、经审阅的网络配置**中加入本次精确官方主机，例如 `oaa.tju.edu.cn`、`gs.tju.edu.cn`、`job.tju.edu.cn`；保留其他既有规则，确认当前 filter mode，不能盲目把模式改成 blacklist 或覆盖整个 DNS 配置。重新解析后，只有得到正常公共地址才再次运行验收；产品仍逐跳检查并校验证书。此交付未修改电脑网络配置。[Mihomo DNS 官方说明](https://wiki.metacubex.one/en/config/dns/#fake-ip-filter)

公共 DNS 给出的不同答案、浏览器能打开、网页搜索工具能读，都不能直接代替采集器在实际运行环境的证明。学校使用校园专网 DNS 或登录门户时，应通过授权的适配器/网关接入并另行验收，不把学校地址加入无边界私网白名单。

## 在未来 Google Cloud 项目中复现

镜像包含同一脚本。已有项目、结算与镜像后，运维可显式创建一个独立、无数据库/桶/SMTP/feed secrets 的验收 Job；不修改在用 worker 的命令。使用与 worker 一致的镜像 digest、地区、VPC egress、防火墙和 DNS 设置，账号只保留该验收所需权限。默认部署脚本不会创建或执行这个 Job。

下面是供未来审阅的命令示例，当前没有执行、没有创建收费资源。替换所有占位值，使用独立的最小权限 probe service account；若生产 worker 使用 VPC，按实际情况补上相同的网络参数。`--max-retries=0` 避免因为预期的问题退出码触发额外抓取。

```powershell
gcloud run jobs create tju-campus-network-check --project=YOUR_PROJECT --region=asia-east2 --image=YOUR_IMAGE_AT_SHA256 --service-account=YOUR_PROBE_SERVICE_ACCOUNT --command=python --args=deploy/cloudrun/probe_sources.py,--network --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=1800s --cpu=1 --memory=1Gi
gcloud run jobs execute tju-campus-network-check --project=YOUR_PROJECT --region=asia-east2 --wait
```

在该执行日志读取脱敏 JSON，逐来源修正 DNS、出口、TLS、权限或 selector 问题后定向复测。抽样不通过导致 Job 失败是预期信号，不能关闭错误退出码来“通过验收”。[Google 创建 Job 文档](https://docs.cloud.google.com/run/docs/create-jobs)、[命令与参数覆盖](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/create)

Google 官方说明 Cloud Run 默认从动态 IP 池访问外部目标。只有学校/授权桥接明确要求出口 IP 白名单时，才评估 Direct VPC egress + Cloud NAT 的静态出口方案和新增费用；固定 `run.app` URL 不是静态出口 IP，也不解决上游登录、验证码或国内用户访问问题。[静态出口官方文档](https://docs.cloud.google.com/run/docs/configuring/static-outbound-ip)、[Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc)

Google ingress 设为 `all` 只表示平台允许来自互联网的请求，IAM 仍独立生效；这些文档没有承诺天津校园网、国内各运营商或微信内置浏览器可达。[Cloud Run ingress](https://docs.cloud.google.com/run/docs/securing/ingress) 正式开放前分别实测校园 Wi-Fi、两种手机流量和微信浏览器的登录、静态资源、原文附件、日历，记录网络/日期/成功率/耗时；同镜像采集 Job 成功也不替代这些学生入口测试。

## 从 GitHub 借鉴的范围

[changedetection.io](https://github.com/dgtlmoon/changedetection.io) 展示了限定 CSS 区域、区分轻量 HTTP 与浏览器抓取、保留错误诊断的思路。本次借鉴“分阶段诊断、HTTP 成功不等于内容成功、只观察相关区域”，代码由本项目独立实现，没有移植其代码，也没有引入额外浏览器服务、自动登录或代理服务。动态网页确有需要时，再为少量经过授权的来源建立独立浏览器适配器，验证网络边界与凭据生命周期；不让任意学生提交的 URL 进入浏览器执行。

不采用“所有来源直接接浏览器”或“失败就切公共 DNS/代理”的默认方案：它们会改变现有安全与运维边界，当前观测也不能证明是必要的解决手段。先在真实部署环境通过本验收，再用产品 pipeline 检验附件、字段证据和审核流程，最后才能做学生试用与时间节省评价。
