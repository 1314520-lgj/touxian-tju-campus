# 偷闲·天大版

天津大学校园通知聚合：把分散在官网、学院网站和微信公众号里的通知抓下来，
解析成结构化事项（截止时间、材料、入口链接），人工复核后发布，支持检索、订阅、
待办与日历导出。

- 后端：FastAPI + SQLAlchemy（SQLite / PostgreSQL），采集走自己的 SafeFetcher
- 前端：React + Vite（`src/campus`、`dist/`）
- 测试：pytest（后端 900+ 用例）+ node --test（前端单测）
- 桌面版：PyInstaller + Inno Setup，双击即用（见 `desktop/`）

## 目录

```
campus-fixed/      产品源码、测试、文档、验证记录
desktop/           桌面版打包（冻结脚本、启动器、Inno Setup 安装脚本、公众号 feed 组件源码）
```

产品内部的接手说明在 `campus-fixed/AGENTS.md`（工程结构、质量门、历史踩坑），
接口契约冻结在 `campus-fixed/campus-contracts/`（v1.5.0，只许增补）。

## 快速开始（源码运行）

```bash
cd campus-fixed
pip install -r requirements-campus.txt
python -m pytest server/campus/tests -q          # 质量门
python scripts/daily_update.py --data-dir data-operator --apply   # 采集 → 复核 → 下架
python serve_cloud.py --mode operator            # 本机运营实例（127.0.0.1:8765）
```

## 桌面版

`desktop/README-exe.md` 有完整的构建与踩坑记录。要点：

- 用 PyInstaller 把 `campus-fixed` 冻结成 onedir，`dist/`、`config/`、
  `campus-contracts/` 必须随包；
- 用 Inno Setup 出安装程序，装到 `%LOCALAPPDATA%\Programs\TouxianCampus`，
  桌面/开始菜单快捷方式；
- 启动器先起公众号 feed 服务，再起本体。

## 公众号链路（为什么不是直接爬）

微信对非登录客户端返回验证页：直连文章页会被重定向到
`/mp/wappoc_appmsgcaptcha`（环境异常），拿不到正文，也拿不到公众号的历史文章列表
（列表需要登录态）。所以本项目的公众号通路是：

**用户自己扫码登录微信读书 → 本机导出 JSON Feed（含全文）→ 产品按普通来源采集**

- 全程本机，不经任何第三方中转；登录凭据只在 `~/.config/weread/`
- 条目 URL 仍是真实 `mp.weixin.qq.com` 原文，正文逐字取自 feed
- 公众号来源一律标「未核验」，每条都要人工复核后才能发布

`desktop/weread-feed/` 就是这个 feed 服务的源码（`server.mjs` 提供
`/feeds/all.json`，`login.mjs` 生成微信扫码登录二维码）。

## 许可

`campus-fixed/LICENSE-Touxian`（产品）与 `campus-fixed/LICENSE-Desktop`（桌面壳）分别适用。
