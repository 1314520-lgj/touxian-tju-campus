# 运营中继操作规程（deploy/operator）

本文件是"你运营"模式的标准作业程序：在**本机与托管沙箱都无法直连 `*.tju.edu.cn`**
（DNS 落到保留地址，产品自身的 SafeFetcher 拒绝抓取——这是任务池 N4 的正确行为）
的前提下，由运营方作为唯一能触达官网的组件，把官方页面**原样**送进产品自己的
授权 feed 管道，让采集、抽取、审核、发布全链路在真实数据上跑起来。

中继是**传输，不是身份**：feed 条目里的 URL 永远是真实官网地址
（`feed_archive.original_url` 强制 https + 公网主机，kind=wechat 还要求
`mp.weixin.qq.com`），候选、证据链接、发布事件全部指向原始公告。
中继不改写、不缩写、不翻译任何正文；每条记录附抓取时间与正文 SHA-256。

## 0. 前置条件

```bash
# 终端 A：中继 feed 服务（仅监听 127.0.0.1）
python deploy/operator/relay.py serve --port 8791

# 终端 B：产品以 operator 模式运行（worker 内置、dev-auth 开、绝不播种合成数据）
CAMPUS_CLOUD_MODE=operator \
CAMPUS_DATA_DIR=./data-operator \
CAMPUS_TRUSTED_GATEWAY_ORIGINS=http://127.0.0.1:8791 \
PORT=8000 python serve_cloud.py
```

- `CAMPUS_TRUSTED_GATEWAY_ORIGINS` 精确命名这一个回环源，SafeFetcher 只放行它；
  该环境变量是给部署管理员的手段，不是绕过安全边界的开关。
- operator 模式默认绑 `127.0.0.1`（`CAMPUS_OPERATOR_HOST` 可覆盖），
  管理员账号 `operator@touxian.local`。**切勿**把该模式直接暴露到公网。
- 若本机虽解析到 fake-IP、但把官方 origin 加进 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`
  后 `SafeFetcher` 能直接抓真实页面，则**不需要中继**：用
  `scripts/campus_collect.py init|collect --gateway-from-config` 直接初始化/日常采集，
  见 `docs/REAL-DATA-RUNBOOK.md`。中继只用于真正无法直连官网的主机。

## 1. 标准循环（一个运营周期）

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| ① 取证 | 人工打开官方栏目页，逐篇复制正文存为 `staging/<栏目>/<标题>.txt` + `.meta.json` | meta 至少含 `url`（真实官网文章页）、`title`、`content_html`、`fetched_at`。**逐字保存，不得改写** |
| ② 构建 | `relay.py stage` → `relay.py build` | stage 校验后生成 JSON；build 产出 `out/feed-<栏目>.json` 与 `provenance.json`（每篇含 sha256/字节数/抓取时间） |
| ③ 注册 | `relay.py register --api http://127.0.0.1:8000 --feed-origin http://127.0.0.1:8791` | 登录 → 每个栏目建/复用 `relay-<栏目>` 源（authorized_feed / authorized_api / authorized_fulltext）→ 记录 verification 依据 → 触发采集。幂等键稳定，重放同一周期只更新同一行 |
| ④ 回收 | `relay.py report` | 写 `cycle-report.json`：中继源健康度 + 待审候选（标题、截止时间、材料、告警、extractor 版本） |
| ⑤ 决策 | `relay.py decide --candidate <candidate_id> --decision accept --reason "理由"` | 人审核每一候选；accept 发出 `candidate.accepted` outbox，worker 发布；replacement_candidate 恒为 null——发布的就是运营方刚读到的抽取结果 |
| ⑥ 验证 | 再次 `report` + 查 `/api/v1/events`、搜索 | 候选离开待审队列、事件发布、证据链接可回溯到真实官网 |

staging 目录即证据源，必须随仓库提交；`out/` 是构建产物（已 gitignore），
`session.json` 是活凭证（已 gitignore，绝不提交）。

发布侧同步设防（第 9 轮新增，`tools/build_release_manifest.py`）：清单构建器
**不读 .gitignore**，因此三类本地产物在构建器里直接排除/拒绝——
`deploy/operator/session.json`（活 cookie，等同签名密钥，**构建直接停止**）、
`data-operator/`（运行中的 operator 实例库）、`deploy/operator/out/`
（可再生 feed 产物）；由 `test_release_manifest_guards.py` 4 例钉住
（先红后绿）。若构建报 refusing，删掉该会话文件重新登录即可。

## 2. 四级验收阶梯

一个中继周期按以下四级验收，任一级不过，周期不算完成：

1. **传输级**：`provenance.json` 每篇 sha256 与暂存文本一致；feed 条目 URL 全部为
   真实官网 https 文章页；无 fetched_at 泄漏进 feed 正文。
2. **采集级**：注册后两个中继源 `health=healthy`、`verification=verified_official`、
   `last_success_at` 推进；fetch 作业 succeeded。
3. **抽取级**：候选的每条截止时间都带 `raw_text` 引文与 `evidence_ids`；
   分钟精度、日期精度、`unknown` 精度分级正确（`campus-rules-1.11`）；
   多日期/歧义日期必须落 `warnings` 并 `review_required=true`。
4. **发布级**：只有经过第 ⑤ 步人审的候选才会发布；发布事件的证据链接回到真实官网；
   搜索可召回；落库的 reviews 记录决策人与理由。

## 3. 踩坑记录（真实发生过）

- **dev-auth 限流**：`/api/v1/auth/challenges` 每邮箱每小时最多 6 个验证码，
  且 30 秒内不重复发。连续跑 `register`/`report`/`decide` 会把自己锁在门外。
  **对策**：`AdminSession` 已把会话 cookie 持久化到 `session.json`——
  首次登录花一个验证码，之后所有子命令用 `GET /api/v1/me` 验证复用。
  若仍被限流，等最早的验证码滑出一小时窗口（每过一分钟恢复一个）。
- **写端点要两个头**：所有写操作必须带 `X-CSRF-Token` **和** `Idempotency-Key`，
  缺失返回 422 `idempotency_required`。中继已按调用点传稳定键：
  create=按身份、mode/verification=按源按天、fetch=按周期、decision=按候选版本。
- **审核队列形状**：`GET /admin/reviews` 返回的是**候选**（`candidate_id`/`revision`），
  不是审核单；决策路径是 `/admin/reviews/{candidate_id}/decision`。
- **Windows CRLF**：不要用 Python `write_text` 改仓库内文件（会整文件改写为 CRLF），
  用编辑工具或字节级写入；sha256 边车同样按字节写。
- **pytest 清理误伤**：sandbox 的 safe-delete 拦截器会向 tee 日志注入标记文本甚至
  杀掉 pytest；跑全套件时用 `TMPDIR` 指向全新空目录。
- **`x-dev-login-code` 响应头是小写**：`dict(headers)` 精确匹配会漏掉它，
  `HTTPMessage.get()` 大小写不敏感没问题。

## 4. 边界（中继不做什么）

- 不渲染、不截图、不解析正文结构——运营方复制什么就送什么；
- 不发明 URL、不补全年份、不推测截止时间；
- 不绕过产品的安全抓取、审核闸门与发布门（accept 之前没有任何东西会发布）；
- 不覆盖产品的 `SafeFetcher` 对保留地址的拒绝（N4 行为保持有效）。
