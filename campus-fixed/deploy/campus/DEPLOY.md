# 偷闲·天大版：Linux 生产部署

这套配置构建校园前端和隔离的 FastAPI 入口，运行 API、独立任务 worker、PostgreSQL 和 Meilisearch。API 仅监听宿主机 `127.0.0.1:8000`，由现有 HTTPS 反向代理对外提供服务；数据库和搜索服务不映射宿主端口。RSSHub、WeRSS 是可选的外部信息源桥接，接入方式见 [ADAPTERS.md](ADAPTERS.md)。

当前交付环境没有 Docker daemon 和 PostgreSQL 服务，因此**没有实际运行 Linux 容器、PostgreSQL 并发或生产 SMTP 联调**。本目录已核对程序入口、环境变量、构建上下文、Compose YAML 和官方镜像摘要；应用的本地功能验证由主项目验收报告记录。完成下述部署验收后，再对学生开放。

## 1. 准备

使用 Linux 主机、Docker Engine + BuildKit、Compose v2，准备真实 HTTPS 域名、TLS 证书和可收信的 SMTP 账号。建议先用 2 核 / 4 GB 内存起步，再按来源量、附件量和实际监控调整。主机必须能连接 Python/npm 包源、镜像仓库、学校公开网站和邮件服务。

运行以下命令前，进入发行包的 `tju-campus/deploy/campus`（原仓为 `desktop/attention-desk/deploy/campus`）目录。Compose 的构建上下文是应用根目录；不要单独复制这个部署目录后直接构建。

```sh
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

分别生成 **3 个不同的随机值**，填写 `.env` 的 `CAMPUS_POSTGRES_PASSWORD`、`CAMPUS_SECRET`、`CAMPUS_MEILI_KEY`。示例采用十六进制，避免数据库 URL 中的密码转义问题。再填写真实 HTTPS origin（只含协议、域名及可选端口）、管理员登录邮箱、SMTP host/from/账号密码。多个 origin 和管理员邮箱用英文逗号分隔。

`.env` 用于生产，不会开启开发验证码或导入演示数据。生产环境强制 PostgreSQL、HTTPS origin、至少 32 字符的应用密钥、关闭开发认证；配置了 SMTP 时要求 TLS。587 端口通常使用 STARTTLS；465 通常设置 `CAMPUS_SMTP_SSL=true`、`CAMPUS_SMTP_STARTTLS=false`，以你的供应商要求为准。未实际收到账户验证码不能视为登录链路验收成功。

不要将 `.env`、用户数据库、原文快照或备份提交到 Git、发到群里或打包给其他开发者。Dockerfile 只复制需要的源码，配套的 `Dockerfile.dockerignore` 会排除运行数据和环境文件。[Docker 官方文档](https://docs.docker.com/build/concepts/context/#dockerignore-files)说明了这种专用忽略文件的行为。

## 2. 构建并准备可信反向代理

```sh
docker compose config --quiet
docker compose build api
docker compose up -d postgres meilisearch
docker network inspect touxian-campus_default --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

若使用宿主机上的 Nginx，容器通常将宿主机代理连接识别为 Docker 网络网关 IP。把上一步得到的实际代理对端 IP 填入 `.env` 的 `CAMPUS_FORWARDED_ALLOW_IPS`，然后再启动 API。若反向代理在其他容器/主机，填写它连接 API 时的实际对端 IP，多个用逗号分隔。该字段决定哪些对端的 `X-Forwarded-Proto` 等头可以被信任；不要为省事改成 `*`，也不要将 API 端口绑定公网。

这是现有 Nginx 的最小站点示例。替换域名和证书路径，并按主机情况检查配置后重载。示例覆盖来自客户端的转发头，关闭请求 URL 日志，避免泄漏日历访问凭据和快照 `read_token`。如需要访问监控，请仅记录脱敏的路由类别、状态和耗时。

```nginx
server {
    listen 443 ssl;
    server_name campus.example.invalid;
    ssl_certificate /etc/letsencrypt/live/campus.example.invalid/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/campus.example.invalid/privkey.pem;
    client_max_body_size 256k;
    access_log off;
    error_log /dev/null crit;
    # Attachment files are bounded again by the API (8 MiB plus form overhead).
    location ~ ^/api/v1/admin/snapshots/[0-9a-fA-F-]{36}/replacement$ {
        client_max_body_size 9m;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
        proxy_buffering off;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
        proxy_buffering off;
    }
}
```

证书续期、HTTP 跳转 HTTPS 和域名解析由现有代理负责。不要启用响应缓存、分析 SDK 的完整 URL 采集或请求体记录，尤其是 `/api/v1/calendar/`、快照文件、认证和个人事项接口。Uvicorn 入口已经关闭访问日志。

## 3. 启动与注册官方来源

```sh
docker compose up -d api worker
docker compose ps
curl --fail https://campus.example.invalid/api/v1/health/ready
docker compose exec api python -m server.campus_app init-sources
```

`init-sources` 只注册 `config/campus-sources.json` 中的官方栏目；不会伪造采集成功或生成校园通知，重复执行不会覆盖已有来源。真正的采集由 worker 调度，或由管理员在“来源管理”中触发。首次来源状态为 `never_checked`。来源健康状态、最近成功时间和错误代码都应根据真实采集结果查看。

登录管理员邮箱，确认收到验证码并进入管理界面。先采集少量来源，核对原文、附件、字段证据和日期，再接受候选事项。管理员可在审核前查看尚未发布的快照；学生只能读取已发布且未撤回的资料。列表网页、正文和附件中的指令不会成为执行命令。

`/api/v1/health/ready` 检查应用与存储可用，并返回演示标记；它**不证明**全部来源、Meilisearch、邮件投递或后台任务健康。Meilisearch 冷启动期间索引任务可能重试，在后台任务列表检查最终状态。站内通知的 `sent` 表示已写入站内收件列表；邮件 `sent` 表示 SMTP 接受，不能代替学生实际收信确认。

## 4. 上线验收

在独立的验收账户上完成以下检查，再邀请真实学生使用：

- HTTPS 首页、静态资源、验证码收信、退出和重新登录成功，浏览器无跨域错误，Cookie 为 Secure。非管理员看不到来源审核入口。
- 填写学院/学段等信息后，公告与订阅范围正确；两名学生的个人事项和状态互相不可读取。
- 官方来源至少完成一次真实采集，查看原文快照/附件，审核一个有证据的候选事项并发布。确认“官方身份已核验”与“采集健康”分开显示。
- 全文搜索返回该原文中的可定位段落，问答带证据引用；证据快照的 `signed_url` 必须使用 HTTPS。如果出现 HTTP，检查代理对端 IP 和转发头。
- 收藏一个事项，修改截止日期后确认版本变化、提醒更新和日历 UID 保持稳定；撤回后学生不可再搜索/读取其正文。
- 确认一条个人事项后，日历、站内提醒及每日摘要符合偏好；删除事项或撤销日历令牌后，旧链接不能继续读出有效事项。
- 测试一封真实提醒邮件，检查静默时段、时区和供应商结果；对 `unknown` 状态人工核查，程序不会自动补发存在重复投递风险的邮件。
- 重启 worker，确认已完成任务不会产生重复事项或重复提醒；在任务列表检查失败任务和待处理队列。

校园事项的业务时区固定为 `Asia/Shanghai`。操作系统采用 UTC 不会改变截止日期和摘要时间。首次 OCR 资源下载、上游微信公众号可访问性和服务器出口网络都需要单独实测。

## 5. 日常维护、备份和恢复

```sh
docker compose logs --tail 100 worker
docker compose logs --tail 100 api
docker compose restart worker
```

Worker 会约每 6 小时检查启用来源，并按学生偏好调度摘要和提醒。任务为持久化队列，失败有次数上限和退避；后台支持有界人工重试。正常停止允许当前任务完成，worker 的宽限期为 12 分钟。进程被强杀时，运行中的任务会等待租约到期再恢复（当前默认最长约 1 小时），不代表任务已经丢失。

备份必须同时保存 PostgreSQL、快照卷、Meilisearch 卷、来源配置及安全保存的 `.env`。下面是 Linux shell 下的一次一致性备份示例；停止 API/worker/Meilisearch 后，PostgreSQL 保持运行以执行逻辑备份。

```sh
mkdir -p backup
chmod 700 backup
docker compose stop api worker meilisearch
docker compose exec -T postgres pg_dump -Fc -U campus -d campus > backup/campus.dump
docker compose run --rm --no-deps -T --user 0 --entrypoint sh api -c 'tar -czf - -C /var/lib/touxian .' > backup/snapshots.tgz
docker run --rm --read-only --user 0 -v touxian-campus_meili-data:/data:ro --entrypoint sh touxian-campus:local -c 'tar -czf - -C /data .' > backup/meili.tgz
cp ../../config/campus-sources.json backup/campus-sources.json
docker compose up -d meilisearch api worker
```

将备份加密保存到受控位置；数据库含学生个人数据，不能当公开示例。单独安全备份 `.env`，应用密钥必须随数据保留，否则旧日历令牌和签名失效。

恢复时在**隔离的新实例与空数据卷**中使用相同版本镜像：先启动 PostgreSQL，用 `pg_restore --no-owner -U campus -d campus` 导入 dump；在 API、worker、Meilisearch 停止时，把快照归档还原到 `touxian-campus_campus-data` 卷、搜索归档还原到 `touxian-campus_meili-data` 卷，并恢复来源配置与 `.env`。快照卷内文件所有者应为 `10001:10001`。再启动服务，按上一节重新验收。不要直接覆盖仍在使用的生产数据卷，也不要运行 `docker compose down -v` 清理带数据的实例。

升级前保存当前代码版本、镜像和完整备份。核对存储迁移说明、锁文件与版本兼容后构建新镜像。回退应同时考虑数据库结构与数据备份，不能保证只换旧镜像即可回退。

## 6. 镜像与验证范围

以下 **multi-platform index digest** 于 2026-10-08 直接从 `registry-1.docker.io` manifest 响应核验；Dockerfile/Compose 同时固定版本标签和摘要。pnpm 固定 `10.30.0`，使用已冻结的 lockfile v9；Python 依赖使用项目 `requirements-campus.lock.txt`。没有使用漂移的 `latest` 标签。

| 镜像 | 固定摘要 |
| --- | --- |
| `node:24.21.0-bookworm-slim` | `sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20` |
| `python:3.12.15-slim-bookworm` | `sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` |
| `postgres:17.11-bookworm` | `sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826` |
| `getmeili/meilisearch:v1.54.3` | `sha256:e68913ab7d6f5b159529e472cfd362ce3c741fafd3c127961b2142abbe41b3c9` |

参考：[Python 官方镜像](https://hub.docker.com/_/python)、[Node 官方镜像](https://hub.docker.com/_/node)、[PostgreSQL 官方镜像](https://hub.docker.com/_/postgres)、[Meilisearch 官方 Docker 文档](https://www.meilisearch.com/docs/guides/deployment/docker)、[Compose 服务配置](https://docs.docker.com/reference/compose-file/services/)。固定版本用于可重复部署，不表示可以永久跳过后续安全更新。

内置解析覆盖 HTML、可提取文字的 PDF、DOCX 和 XLSX。基础容器未安装服务端 OCR/Docling，图片和扫描 PDF 会真实标记失败或部分完成并进入人工处理；学生截图导入使用浏览器本地 OCR，原图不会上传到本服务。真实上游认证、微信公众号授权刷新、生产 SMTP、PostgreSQL 和 Linux 容器仍须由部署方按本说明联调。
