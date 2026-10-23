# 信息源与微信公众号桥接

本项目自己实现受限 HTTP 采集、正文/附件解析、证据、候选审核和事项合并。`rsshub` 与 `werss` 适配器读取**已经部署好的外部 RSS/Atom feed**；Compose 不会自动安装这些项目，也不会自动取得微信授权、学校登录态或公众号管理权限。

可选上游项目为 [RSSHub](https://github.com/DIYgod/RSSHub) 和 [WeRSS / rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)。2026-10-08 核验时，RSSHub 的 [LICENSE](https://github.com/DIYgod/RSSHub/blob/master/LICENSE) 为 AGPL-3.0，WeRSS 的 [LICENSE 原文](https://github.com/rachelos/we-mp-rss/blob/main/LICENSE) 标注 MIT。这里使用 HTTP feed 接口，没有复制、合并或随交付包分发这两个项目的代码；独立部署它们时按所选版本的许可证和上游说明执行。

## 适配器实际能力

| `adapter` | 读取格式与配置 | 边界 |
| --- | --- | --- |
| `website` | 栏目 HTML 链接；`list_selector`、`article_url_pattern` | 默认匹配 `/info/数字/数字.htm`，学校栏目变版需修改对应配置 |
| `rss` | RSS 或 Atom | 默认按链接抓正文；已核验授权来源可显式启用订阅全文 |
| `rsshub` | RSSHub 生成的 RSS/Atom | 本服务不决定上游路由，填实际测试通过的 feed URL |
| `werss` | 推荐 WeRSS 完整 XML/RSS | 扫码授权和刷新由上游维护；全文模式读取 content:encoded，保留微信原链接 |
| `authorized_api` | JSON 数组或 `{"items":[...]}` | url/link、title、published_at/date_published；全文模式读取显式 content_html |

`kind` 描述来源类别，可用 `website`、`rss`、`wechat`、`authorized_feed`；它和 `adapter` 是不同字段。公众号通过 feed 接入时，可用 `kind=wechat`、`adapter=werss`。

请求有字节、连接时间、重定向和文档数量限制。每轮发现最多 100 条，每批处理 8 条、每篇最多 4 个附件；持久化队列继续补采，最多保留 500 个已知条目。超过发现窗口的历史仍需运营安排有界补采，不宣称全历史覆盖。正文解析覆盖 HTML、文本 PDF、DOCX、XLSX，图片和扫描件的服务端 OCR 未配置时会显示失败/部分成功，需要人工处理。学生自己的群通知截图可用前端本地 OCR 导入，并确认结构化字段。

## 1. 先验证外部 feed

由运营人员在独立桥接服务上配置订阅，核对公众号真实身份和所属单位，再取得真实 feed URL。公众号名称相同不能当成身份已核验；`source_identity` 填已核实的公众号标识，并把对应学校官网链接或单位确认记录写到审核依据。

链接模式下，上游返回的文章链接必须能被本服务直接读取。全文模式下，运营先确认供稿权限和完整正文、选择 `feed_content_mode=authorized_fulltext` 并重新核验，再由订阅供稿形成快照；不请求微信原页来伪装验证。只有摘要时明确失败，不自动改用摘要。完整配置与 RSSHub/WeRSS 实际格式差异见 [公众号全文指南](../../WECHAT_FEED_GUIDE.md)。来源 Bearer token 只送给**栏目/feed 请求**；文章、附件和跨 origin 重定向不会继承凭据。本服务没有绕过登录或反爬的功能。

地址中不能包含凭据：不要把 token 放在 `origin_url` 的用户名、密码、query 或路径中，URL 会用于来源展示和审核。需要受保护 feed 时，让你控制的反向代理接受 `Authorization: Bearer ...`；不要假定任意上游本身支持这种认证方式。

## 2. 管理员创建来源

在管理界面新增来源。下例是 `SourceCreate` 的合法字段示例，示例域名和身份标识必须换成真实值；可选 `feed_content_mode` 默认 `link`：

```json
{
  "name": "某学院公众号 · 运营确认后填写",
  "kind": "wechat",
  "origin_url": "https://feeds.example.invalid/tju/college.xml",
  "adapter": "werss",
  "source_identity": "operator-confirmed-account-id"
}
```

接口为 `POST /api/v1/admin/sources`，需要管理员登录、CSRF 和 `Idempotency-Key`；界面会处理这些要求。创建后返回实际 `source.id`，初始身份为 `unverified`、健康为 `never_checked`。管理员用核验接口/界面写入可追溯依据，身份可以为 `verified_official` 或 `verified_organization`。身份核验不会伪造抓取健康状态。

`SourceCreate` **没有 `config_ref` 字段**，不要向 API 提交它或其他额外配置字段。源码配置通过下面的实际机制关联。

## 3. 对应来源的解析配置和 token

应用根目录中的 `config/campus-sources.json` 的顶层是 `{"schema_version":"1.0","sources":[...]}`。保留既有官方来源，把需要的配置项加入 `sources` 数组，按实际来源的 `id` 精确匹配。以下 UUID 仅供说明，必须替换成上一步创建来源返回的值：

```json
{
  "id": "f6a3b540-8650-49dd-9db1-2ddaa786c641",
  "config": {}
}
```

网站栏目可这样配置：

```json
{
  "id": "实际的来源UUID",
  "config": {
    "list_selector": "a[href]",
    "article_url_pattern": "/info/\\d+/\\d+\\.htm"
  }
}
```

RSS/Atom 通常不需要额外解析配置。配置项只是已创建来源的解析设置，不代表新增来源已通过官方核验；初始化来源命令只用于内置官方种子。现有数据库必须与配置中的 UUID 对应，迁移/恢复时一并保存数据库和配置文件。

如 feed 需要 Bearer token，在部署目录 `.env` 加入下面的变量。规则是来源 UUID **去掉横线、转大写**，前缀 `CAMPUS_SOURCE_`，后缀 `_TOKEN`：

```dotenv
CAMPUS_SOURCE_F6A3B540865049DD9DB12DDAA786C641_TOKEN=真实的服务端feed令牌
```

程序会在 feed 请求中添加 `Authorization: Bearer <token>`。凭据只留在服务器环境文件，不写到来源 JSON、浏览器或原文链接中。Compose 的 `env_file` 会将这种动态来源变量传给 API 和 worker；修改 `.env` 后运行 `docker compose up -d --force-recreate api worker` 使新环境生效。配置 JSON 以只读 bind mount 提供给两者，修改配置文件不需要重建镜像。

## 4. 内网网关的显式信任

默认采集器只访问公开 HTTP/HTTPS 地址，逐跳检查 DNS 和重定向，固定连接到通过检查的 IP，并按原主机名验证 TLS；不使用系统代理环境变量。普通公开源不需要配置白名单。

如果你自己的 feed 桥接在内网，添加**完整、精确的 origin**（协议、主机、可选端口），例如：

```dotenv
CAMPUS_TRUSTED_GATEWAY_ORIGINS=http://feed-bridge.internal:8001,https://feeds.school.example
```

不填 path、通配符、账号或 token。只为实际控制的桥接 origin 开放；这是对该 origin 的私网访问例外。不要填学校所有私网、宿主机管理接口、数据库、云元数据地址或学生提交的任意 URL；不支持用它连接云元数据。非标准端口也要求对应 origin 被显式允许。若文章链接指向另一个私网 origin，它不会继承 feed 的例外，必须独立评估和配置。不同 origin 的重定向不会携带 feed token。

私网 DNS、容器网络和证书仍需部署方正确配置；只添加字符串白名单不会使不可达的主机变得可达。Docker Compose 不会自动创建名为 `feed-bridge.internal` 的服务。修改信任列表需重建 API/worker 的运行环境。

## 5. 触发采集和上游变更提示

管理员/运营可在“来源管理”点击采集，也可调用 `POST /api/v1/admin/sources/{source_id}/fetch`（登录、CSRF、幂等键）。请求仅把任务入队；查看任务和来源健康，不能将 HTTP 202 当作采集成功。定时 worker 默认约每 6 小时检查启用来源；`CAMPUS_SOURCE_REFRESH_SECONDS` 可设为 300..86400 秒，Cloud Run 材料默认 1800 秒，每分钟运行队列任务来推进解析、提醒和到期检查。

## 学生选择和申请接入

学生在“信息来源”中直接选择已启用来源；新来源可提交公开网站地址、公众号名称或公开微信文章链接。申请需要本人登录，不创建全局来源、不取得私网访问例外、不存储微信会话参数。运营先完成本指南的创建、核验和采集，再在“接入申请”连接到健康且有成功记录的同类来源。未接通时填写原因；只有申请者明确选择时才自动关注。

### WeRSS 管理接口核验备注（2026-10-08）

当前上游源码的管理前缀为 `/api/v1/wx`，可通过 `/api/docs` 或 `/api/openapi.json` 核对实际部署版本。公众号搜索为 `GET /api/v1/wx/mps/search/{kw}`，文章识别为 `POST /api/v1/wx/mps/by_article?url=...`，管理接口使用上游 JWT 或 AK-SK 授权；它与本系统的 Bearer feed token 是不同用途。以部署版本的 [apis/mps.py](https://github.com/rachelos/we-mp-rss/blob/main/apis/mps.py) 和 [core/base.py](https://github.com/rachelos/we-mp-rss/blob/main/core/base.py) 为准，不直接照 README 中不同路径假定可用。

RSS 路径可为 `/feed/{mp_id}.xml` 或 `/rss/{mp_id}`；当前 [apis/rss.py](https://github.com/rachelos/we-mp-rss/blob/main/apis/rss.py) 中 RSS 鉴权依赖未启用，不要把管理接口有授权误认为 feed 已受保护。使用自控网关或私网边界保护 feed。`RSS_LOCAL=True` 可让文章指向桥接缓存页，但仍须实际抓取并核对全文和证据；仅打开 `RSS_FULL_CONTEXT` 无法改变本应用仍按文章链接抓正文的行为。WeRead 备用方式有篇数和授权续期限制，不能承诺公众号历史齐全或持续无遗漏。

如获授权的外部服务需要主动通知变更，设置独立的 `CAMPUS_CHANGE_TOKEN`（至少 32 字符），再调用：

```http
POST /api/v1/internal/changes
Authorization: Bearer <CAMPUS_CHANGE_TOKEN>
Idempotency-Key: provider-event-123
Content-Type: application/json
```

```json
{
  "source_id": "f6a3b540-8650-49dd-9db1-2ddaa786c641",
  "source_url": "https://feeds.example.invalid/tju/college.xml",
  "observed_at": "2026-10-08T09:00:00+08:00",
  "provider_change_id": "provider-event-123"
}
```

`source_url` 的域名及端口必须与已注册来源一致；这里只是重新抓取该来源的提示，不接收任意文章内容，也不直接发布事项。同一变更使用稳定的幂等键。该服务凭据和来源 feed token 是不同用途，分别生成。

## 6. 验证失败时的处理

- `auth_expired`：feed 上游返回 401/403，检查桥接授权、网关 Bearer token 与上游帐号状态；修复后再采集。
- `unsafe_address` / `unsafe_port`：DNS 或端口不满足安全规则。先核对真实 DNS 与网关归属；只有自营桥接才配置精确例外，不要把未知代理地址直接加入信任列表。
- `empty_feed_or_layout_changed` / `invalid_feed`：检查 feed 格式、栏目选择器、链接规则和上游返回内容，避免把登录页当 feed。
- `degraded`：某些正文/附件失败，即使栏目请求成功也不能视为资料完整；查看候选的解析警告与缺失证据。
- OCR 未配置、时间不明确或证据不足：进入人工补全，保持“待确认”，不凭猜测生成精确截止时间或自动发布。

本次开发环境的部分学校域名被本机代理 DNS 映射到 `198.18.0.0/15`、`2001:2::/48` 等测试地址，服务按规则拒绝。网页工具确认来源身份与栏目存在，不能代替本服务在目标服务器上的真实采集。上线时使用可验证的公共 DNS，先以真实健康记录验收，不要为演示放宽 SSRF 检查。

公众号在本系统中是**信息输入来源**。当前提醒渠道为站内和已配置 SMTP 邮件；没有承诺自动发微信公众号消息、小程序订阅消息或企业微信推送。
