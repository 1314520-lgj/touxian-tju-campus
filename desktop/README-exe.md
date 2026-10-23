# 桌面版 / 安装程序（偷闲·天大版）

产品树（`../campus-fixed`）+ **本机公众号 feed 服务** 冻结成一个 Windows 安装程序：
装完桌面有快捷方式，双击 = 起本机公众号 feed + 起偷闲 + 开浏览器。

## 公众号为什么是本机 feed（而不是第三方中转 / 直接爬）

实测（2026-10-23）：

| 路线 | 结果 |
|---|---|
| 产品 SafeFetcher 直连公众号文章 | 被重定向到 `/mp/wappoc_appmsgcaptcha`（环境异常），按纪律拒绝 |
| 普通 HTTP 客户端 | 200 但只有 18 KB 验证页，正文 0 字 |
| 云端渲染（Firecrawl） | 能拿单篇正文，但拿不到账号的历史文章列表，且属借第三方绕过风控 |
| 第三方中转 `weread.111965.xyz`（WeWe RSS 依赖） | 当天 502（Cloudflare 源站故障），备用域名 404 |
| **微信读书官方扫码登录 + 本机导出 feed** | **可用**：官方 qrconnect → `i.weread.qq.com` 取 token → 本机写 JSON Feed |

所以公众号链路改成「用户自己扫码登录微信读书 → 本机导出 JSON Feed → 偷闲按普通来源采集」，
全程不经第三方中转，凭据只在本机 `~/.config/weread/`。

## 产物

```
../exe-dist/
  TouxianCampus-Setup-1.0.0.exe   # 安装程序（135.5 MB）
  TouxianCampus/                  # 偷闲主程序（PyInstaller onedir）
  weread-feed/                    # 公众号 feed 组件
    server.mjs                    #   本机 HTTP 服务（4011），返回 /feeds/all.json
    login.mjs / login.bat         #   微信扫码登录，生成 qr png 并打开
    runtime/node/node.exe         #   自带 node 运行时不依赖系统环境
    node_modules/                 #   weread-omni + 依赖
    feed.json                     #   缓存（首次可预置，之后后台刷新）
  start.bat / launcher.ps1        # 启动器（ASCII 文件名）
```

安装位置 `%LOCALAPPDATA%\Programs\TouxianCampus`；桌面 + 开始菜单快捷方式；可卸载。

## 构建

```powershell
pwsh -File build.ps1                                              # 1) 冻结偷闲
# 2) 准备 weread-feed（见下）
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss    # 3) 出安装程序
```

### weread-feed 组件怎么来

```powershell
mkdir weread-feed && cd weread-feed
npm install weread-omni qrcode --no-audit --no-fund       # 164 MB 依赖，含 PDF/书籍相关包
# server.mjs / login.mjs / login.bat 见 ../exe-dist/weread-feed/（同目录版本）
mkdir runtime\node && copy "C:\Program Files\nodejs\node.exe" runtime\node\node.exe
```

`server.mjs` 只做两件事：立即返回缓存的 `feed.json`；后台按需重建
（`weread-omni public-accounts feed subscriptions --format json --limit 100 --refresh`）。
首次导出约 60 篇要几分钟，之后每 6 小时刷新。

## 已解决的坑（都实测过）

1. **PDF 解析子进程**：产品用 `subprocess.run([sys.executable, "-c", script])`；冻结后
   `sys.executable` 就是 exe，子进程会变成再起一个服务实例。入口识别 `-c` 并 `exec`。
2. **justext 停用词表**：`trafilatura` 的 justext 救援要读包内 stoplists，
   PyInstaller 默认不收 → `--collect-all justext`（另加 `htmldate`）。
3. **前端与契约**：`dist/`、`campus-contracts/`、`config/` 必须 `--add-data`。
4. **来源补齐**：启动时每次 `sources.seed()`（幂等），否则新订阅的公众号重启不生效。
5. **启动器编码**：`cmd.exe` 按控制台代码页解析 `.bat`，UTF-8 中文文件名会乱码
   （实测「不是内部或外部命令」）→ `.bat/.ps1` 用 ASCII 名 + UTF-8 BOM。
6. **设置文件 BOM**：产品 `json.loads` 不吃 BOM，启动器写完要去掉。
7. **安装程序路径**：从 `D:\...\默认工作区\...` 直接运行会退出码 1，从桌面/下载目录正常。

## 实测记录（2026-10-23）

- 扫码登录：微信读书账号 `938531528` 登录成功（官方 qrconnect，非第三方）。
- 公众号：账号里已有 **21 个**天大相关公众号订阅；导出 feed **59 篇**含全文。
- 装后运行：偷闲 8765、feed 服务 4011 均在听；**35 个来源**（34 官网 + 1 公众号），
  采集 526 份快照、384 条候选，公众号来源 health=healthy。
- 公众号候选示例：《天津大学2027年博士研究生招生简章发布！》（抽出 3 个截止时间）、
  《2026年冬装发放慰问活动物资采购公告》（截止 2026-11-05）等，均为 pending 待人工复核。

## 已知限制

- **部分公众号文章没有独立 <h1>**，卡片标题会退回账号名（如「天大学生资助」）。
  正文与 URL 正确，只是标题需要人工改；可在后续版本用 feed 标题兜底修复。
- 公众号取决于微信读书账号的订阅；要抓的号没订阅就先在微信读书里订阅。
- 官网覆盖 34 个栏目；研招网（yzb）因文章页无独立标题仍未注册。
- 安装程序未签名，SmartScreen 可能提示未知发布者。
- 微信读书登录态会过期；过期后运行 `weread-feed\login.bat` 重新扫码。
