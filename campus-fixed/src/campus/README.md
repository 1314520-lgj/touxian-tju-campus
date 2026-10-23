# 天大版 Web 前端（A7）

入口：`campus.html`，React 18/Vite，所有正式数据由 `/api/v1` 获取。构建/代理/依赖由 A0 在公共配置中装配。本目录不触碰原聊天工具入口。

## 已实现

- 手机和桌面自适应：今天、机会、找信息、我的事项、信息来源、个人设置、管理工作台。
- 邮箱 OTP 会话、集中 Cookie/CSRF client。开发验证码只在 localhost 响应明确 X-Dev-Login-Code 时展示。
- 公开事项原文依据、快照、历史版本、办理入口；个人 read/save/state 的乐观版本控制，409 提示并重读。
- 群文字规则提取、浏览器 Tesseract OCR（8 MB 和 2000 万像素限制）、核对表单；没有任何原文或截图后台上传接口。只 POST 确认的 PersonalItemDraft，摘录默认 null。
- 画像、订阅、提醒频率、免打扰、简报和真实投递记录；生成/撤销 ICS 订阅凭证。
- operator/admin 可读管理来源、手动抓取、查看任务与重试、全文证据审核；admin 可接入/修改/核验来源、管理公开/撤回。
- 每个列表提供真实加载、空状态、失败与翻页。不会生产隐式 Mock。health/ready.demo_data 为 true 时显著标注合成演示。

## 测试

从 desktop/attention-desk 执行：
```powershell
node --test src/campus/tests/*.test.mjs
```

覆盖本地隐私 DTO、日期精度与相对日期、时区、重复日期、非法日期、URL、Cookie/CSRF/幂等、409 和网络失败。此为模块单元测试，真实构建与浏览器端到端由 A0 装配后执行。

OCR 使用 lazy import `tesseract.js`。可由部署方设置 `window.CAMPUS_OCR={workerPath,corePath,langPath}` 使用自托管组件；未配置时按库默认地址下载模型。识别失败保留手动文本路径，始终需用户确认。

## 真实浏览器验收

`src/campus/tests/browser.e2e.mjs` 使用 Chromium，拒绝访问非本机服务，且只有 `health/ready.demo_data=true` 才会执行写操作。运行前先启动独立合成演示，不可连接正式学校数据。

```powershell
$env:CAMPUS_E2E_URL='http://127.0.0.1:8000'
$env:CAMPUS_E2E_OCR='true'
$env:CAMPUS_E2E_OUTPUT='campus-e2e-output'
node src/campus/tests/browser.e2e.mjs
```

测试环境需要 Playwright 与 Chromium；可设置 `PLAYWRIGHT_MODULE` 为已安装的 Playwright 模块路径。本机 Codex 运行时提供备用路径，其他电脑按其实际安装配置。管理邮箱默认 `demo-admin@tju.edu.cn`，可通过 `CAMPUS_E2E_ADMIN` 指定演示管理员。重复执行时同一邮箱验证码仍受服务的 30 秒间隔与每小时限制，不绕过认证限制。

脚本验证 1440 px 桌面与 390 px 手机、OTP 登录、真实学院目录、收藏及完成/重新待办、证据与签名快照文件、全文搜索与带引用问答、个人导入的默认隐私边界、真实中文 OCR、ICS 生成撤销、管理撤回恢复，捕获浏览器异常与服务 5xx。结果为 `browser-report.json` 和 PNG 截图。OCR 会下载 `chi_sim+eng` 识别模型；未设置 `CAMPUS_E2E_OCR=true` 时报告会明确标记未验 OCR。
