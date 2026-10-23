# t18：智能与计算学部整源 `unsafe_address` 的定位与合规修复

## 结论（一句话）

该来源的**文章正文都在受信主机上、能正常抓**，报 `unsafe_address` 的是文章里的**附件**——
它们放在另一个官方主机 `yzb.tju.edu.cn`（天津大学研究生招生网）上，而这个 origin 不在
`--gateway-from-config` 推导出的受信网关列表里，本机 TUN 又把 `*.tju.edu.cn` 解析成 fake-IP，
于是 SafeFetcher 按设计拒绝。合规修法只有一个：把该官方 origin 显式列入受信网关。
已在 `config/campus-sources.json` 里声明该官方来源（受信列表 12 → 13 个 origin），
**没有改动、也没有放松 SafeFetcher 的地址安全判定**。

修复后（data-operator 的副本上实跑）：该源 15 条待采项 **14 条完成**，6 个 yzb 附件实测抓回归档，
新快照 6 篇；错误从 `unsafe_address ×7` 降到 `×1`。剩下的那 1 条是原始通知里的一条
**2015 年明文 http 死链**（同一路径在 http 与 https 下都是 404，文件已不存在），
在任何合规前提下都取不回来（见「为什么最后一条不该修」）。

## 一、现场事实（2026-10-21 07:30 无人值守运行）

- 日志：`<数据目录>/logs/daily-20261021.log` → `失败来源  天津大学智能与计算学部 · 通知公告：unsafe_address ×7`
- 该来源行：`status=degraded`、`errors=7× unsafe_address`、`health=degraded`、`last_error_code=unsafe_address`；
  采集 backlog 15 条（13 completed / 2 failed），两条 failed 的 item 级错误码是
  `attachment_fetch_unsafe_address`（attempts=7，`next_retry_at` 已过）：
  - `https://cic.tju.edu.cn/info/1041/6176.htm`（智能与计算学部关于开展2026年硕博连读研究生选拔的通知）
  - `https://cic.tju.edu.cn/info/1041/6006.htm`（招收2026级推荐免试研究生（含直博生）工作的说明）
- 当次受信网关：`config/campus-sources.json` 推导出 **12 个 origin**，含 `https://cic.tju.edu.cn`，
  **不含** `https://yzb.tju.edu.cn`。

## 二、根因证据

### DNS（本机 Clash TUN 的 fake-IP）

| 主机 | 解析结果 | 在受信列表 |
|---|---|---|
| cic.tju.edu.cn（来源自己的 origin） | 198.18.0.205 | 是 |
| oaa.tju.edu.cn（同类正常来源） | 198.18.0.200 | 是 |
| **yzb.tju.edu.cn（附件所在主机）** | **198.18.0.206** | **否** |

`resolve_public()` 的判定：地址不是全局可路由（fake-IP）且该 exact origin 不在受信列表 → `unsafe_address`。
这正是这套机制存在的意义，拒得对。

### HTTP 探测（附件本身可达）

- `GET https://yzb.tju.edu.cn/` → 200（57 KB，标题「天津大学研究生招生网」）
- `GET https://yzb.tju.edu.cn/xwzx/tztg/` → 200，20 条原生文章链接（如 `…/xwzx/tztg/202603/t20260323_324574.htm`）
- 附件样例 `GET https://yzb.tju.edu.cn/xwzx/tkbs_xw/202510/W020251020345725820350.doc` →
  200 / `application/msword`
- 两条失败文章的附件链接（都在 yzb，均来自正文）：
  - 6176（硕博连读）：3× https + 1× 旧 http
  - 6006（推免说明）：2× https + 1× http（2015）

### 用产品自己的 SafeFetcher 复现（不改任何安全规则，只换白名单）

| 用例 | 结果 |
|---|---|
| 1. `https://…W020251020345725820350.doc` + 配置推导列表（12） | **OK**，6,541,312 字节 `application/msword` |
| 2. `http://…201507/W020150724389805979600.doc` + 同一列表 | `FetchError(unsafe_address)` |
| 3. 同一 http 链接 + 额外把 `http://yzb.tju.edu.cn` 加进白名单 | `FetchError(upstream_http_404)` |

## 三、修法（只动 config/）

在 `config/campus-sources.json` 末尾新增一个官方来源条目：

```json
{
  "id": "5a77883c-9332-5fcf-9d58-d44d3a5404e8",
  "name": "天津大学研究生招生网 · 通知公告",
  "kind": "website",
  "origin_url": "https://yzb.tju.edu.cn/xwzx/tztg/",
  "adapter": "website",
  "source_identity": "yzb-notices",
  "verification_basis": "2026-10-21 服务端抓取核验：… https://yzb.tju.edu.cn/xwzx/tztg/",
  "config": {
    "list_selector": "a[href]",
    "article_url_pattern": "^https?://yzb\\.tju\\.edu\\.cn/(?:info/\\d+/\\d+\\.htm|xwzx/[a-z_]+/\\d{6}/t\\d+_\\d+\\.htm)(?:[?#]|$)"
  }
}
```

- id 是按仓库既定的确定性规则算出来的：`uuid5(NAMESPACE_URL, "tju-campus-source:" + origin_url)`。
- 效果：`gateway_origins_from_config()` → **13 个 origin，含 `https://yzb.tju.edu.cn`**。
- **不会**多出一个被采集的来源：`builtin_sources()` 用 `server/campus/sources.py` 里的 `BUILTIN_ORIGINS`
  硬白名单过滤配置条目，新条目不在其中 → 注册的仍是 20 个来源（实测 `registration: new 0, existing 22`，
  builtin 仍 20）。它的作用就是「声明一个官方 origin，供受信网关使用」。
  若将来想真正把它作为常规来源采集，需要在 `server/campus/sources.py` 的 `BUILTIN_ORIGINS`
  里加一行（本轮 inScope 不含 server/，未做）。

## 四、修复后的实测（data-operator 的副本，`--apply`；原目录只读）

```text
网关 origin 数：13（含 https://yzb.tju.edu.cn）
天津大学智能与计算学部 · 通知公告：
    new_snapshots 6，parse_attachment ok 1 / failed 5（.doc 旧格式解析失败，与地址安全无关）
    errors 1× unsafe_address（仅剩下面那条死链）
采集 backlog：15 条 → 14 completed / 1 failed（6176 已 completed）
新归档的 yzb 附件 6 个：4× .doc、1× .docx …（示例 https://yzb.tju.edu.cn/xwzx/tkbs_xw/202510/W020251020345726014331.docx）
真实标题示例：「智能与计算学部关于开展2026年硕博连读研究生选拔的通知」
```

也就是说：**6 条因 fake-IP + 未受信而失败的附件全部抓回来了**，来源的文章本身一直都能采。

## 五、为什么最后一条不该修

- 该 URL：`http://yzb.tju.edu.cn/xwzx/zxxx/201507/W020150724389805979600.doc`
- 直接探测：`http` → **404**；同一路径 `https` → **404**；而 `http://yzb.tju.edu.cn/` 根路径 → 200（主机可用）。
- 结论：**文件已经不存在**，这是原始 2015 年通知里的一条历史死链。就算把 http scheme 也加进受信列表
  （`config` 路径做不到——`test_source_catalog.py` 要求每个来源的 `origin_url` 必须是 https），
  它也只会以 `upstream_http_404` 失败。
- 所以：不动安全判定，也不动配置，如实记录。**唯一不该做的事**就是把 SafeFetcher 的地址判定放宽来
  让这个错误码消失。

## 六、其它来源的同类检查（一并列出）

1. **20 个注册来源自己的 origin 全部覆盖**：配置推导出的 11 个 tju 主机 + `mp.weixin.qq.com`
   就是全部注册来源的 origin，没有缺口。
2. **实际抓取过的文档全部落在受信主机**：job / gs / ico / cic / eee / news / me / oaa / zs / yx / scet /
   mp.weixin（按 data-operator 里已归档文档的 `source_url` 主机统计）。唯一未受信的是 yzb（本轮已修）。
3. **同类但不同错误码**：两个「运营中继」来源在 `--gateway-from-config` 下必然报 `feed_https_required`，
   因为它们的 origin 是 `http://127.0.0.1:8791/…`——既不在 `config/campus-sources.json`（由 relay 单独注册），
   也不在推导出的受信列表里。中继场景需要显式设置 `CAMPUS_TRUSTED_GATEWAY_ORIGINS`（含该 loopback origin），
   或者让 feed 走 https。**这不是本轮的修复对象，但同属「受信网关没覆盖到已声明 origin」这一类。**
4. **只作入口、从不抓取的主机**（不会触发 `unsafe_address`，但将来若注册成来源需要同样处理）：
   `classes/glearning/pan/payment/sszs.tju.edu.cn`、`job.twt.edu.cn`、`coursera.twt.edu.cn`、
   `202.113.8.92`、`172.19.241.3`，以及第三方 `chsi.com.cn`、`chaoxing.com`、`jinshuju.com/net`、
   `feishu.cn`、`meeting.tencent.com`、`xinhuaxmt.com`、`neoscholar.com`、`tufts.edu` 等。

## 七、复现命令与实测结果

```powershell
# 1) 受信网关推导（应含 https://yzb.tju.edu.cn）→ 实测 13 个 origin
python -c "import sys; sys.path.insert(0,'.'); from scripts import campus_collect as c; print(c.gateway_origins_from_config())"

# 2) 真实日常更新（契约给的命令形式）
python scripts/daily_update.py --data-dir .test-temp-source-check --gateway-from-config
#    实测：退出码 0；打印「受信网关：config/campus-sources.json（13 个 origin）」「来源注册 新增 20 · 复用 0」。
#    注意：数据目录不存在时这是**首次运行的演练计划**（missing_directory=True，只计划不抓取）；
#    要看真实抓取结果必须 --apply，或用 data-operator 的副本，见下。

# 2b) 真实抓取（对 data-operator 的副本，--apply；原目录全程只读）
python scripts/daily_update.py --data-dir <data-operator 的副本> --gateway-from-config --apply --json-out daily.json
#    实测：网关 13 个 origin；「天津大学智能与计算学部 · 通知公告」new_snapshots=6、
#    backlog 15 条 → 14 completed / 1 failed（6176 已由 failed 转 completed）、
#    errors 从 7× unsafe_address 降到 1×（仅剩第五节那条 2015 死链）。

# 3) 全量回归（本机必须带 PYTHONUTF8=1，见下）
$env:PYTHONUTF8='1'
python -m pytest server/campus/tests -q --basetemp=.test-temp-source-gate/basetemp
#    实测：885 passed / 8 skipped / 0 failed / 134 subtests，退出码 0（2026-10-21 09:06）
#    不带 PYTHONUTF8 时：879 passed / 8 skipped / 2 failed（都是 test_prune_expired_command 的解码假错）
#    与本改动直接相关的子集（catalog / 竞争栏目 / collect 脚本 / daily_update / network probe /
#    pipeline / prune）单跑：81 passed / 68 subtests，退出码 0。
```

**关于不带 `PYTHONUTF8` 时的 2 个失败用例**：`test_prune_expired_command.py` 是把
`campus_collect.py prune` 当子进程跑并把 stdout 按 UTF-8 解码；本机默认区域设置是 cp936，
子进程把中文按 GBK 写进管道 → 解码失败 → 断言里 `result.stdout is None`。
带 `PYTHONUTF8=1`（`start-campus.ps1` 就会设这个变量）全部通过。**与本轮 config 改动无关**
（prune 分支不使用受信网关），但值得记一笔：在这台机器上裸跑全量套件会假报这 2 个失败。

另：本轮全量回归期间有队友正在改 `server/campus/sources.py` 与其新测试
`test_blocked_retry_cadence.py`（08:59–09:00），中间一次运行出现过 1 个属于他们改动的失败；
09:06 的最终运行已经全绿（885 passed）。

## 八、未验证 / 边界

- 只在本机（Clash TUN → `*.tju.edu.cn` 全是 198.18.x.x fake-IP）验证。直连校园网的环境不需要受信网关，
  行为不同，未验证。
- 没有在真实的 07:30 计划任务上重跑一次（改的是配置，下一次运行自然生效）；本轮用 `data-operator` 的
  **副本** + `--apply` 实测，原 `data-operator` 只做只读查询。
- `.doc` 旧格式的 `parse_attachment` 失败（5 条）属于另一类问题（正文抽取/解析），本轮未处理。
- 新条目「只做受信声明、不被采集」这一点依赖 `BUILTIN_ORIGINS` 白名单的现有行为；若后续把它提升为
  常规来源，需要同步更新 `server/campus/sources.py` 与文档里的来源计数。