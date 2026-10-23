# 微信公众号 RSS 接入（微信读书路线）

本产品**不登录微信、不保存微信凭证、不破解验证码、不抓取私有内容**。
公众号文章列表与正文在微信侧对普通客户端返回「环境异常 / 完成验证后继续访问」，
本产品对此的既定处置是识别并拒绝（`article_redirected_off_pattern`），不换 UA/代理重试。

能拿到公众号正文的合规做法只有一条：**你自己运行的第三方工具**（WeWe RSS 等，
基于微信读书扫码）把公众号文章抓成 feed，本产品只读这个 feed。凭证完全留在你运行的
第三方工具里，本产品只接触一个本机 feed 地址。

## 一、准备

1. 运行一个 WeWe RSS 实例（Docker 或源码），用微信扫码登录**微信读书**账号，
   订阅你关心的天大官方公众号，并打开**全文模式**（`FEED_MODE=fulltext`）。
2. 确认 feed 可访问且带正文，例如：
   `http://127.0.0.1:4000/feeds/all.json` 或 `http://127.0.0.1:4000/feeds/<id>.atom`。
3. feed 条目必须具备：真实 `https://mp.weixin.qq.com/s/...` 链接 + 正文 HTML。
   只有标题/摘要的 feed 会被拒绝（`wechat_feed_requires_fulltext`）。

## 二、在桌面版 exe 里开启

编辑 exe 同目录的 `touxian-settings.json`：

```json
{
  "wechat_feed_enabled": true,
  "wechat_feeds": [
    {"name": "天津大学教务处", "url": "http://127.0.0.1:4000/feeds/all.json"}
  ]
}
```

重启 exe。程序会：

* 环境变量 `CAMPUS_WECHAT_FEED_ENABLED=true`、`CAMPUS_WECHAT_FEEDS=名称=地址;…`
* 把 feed 的 origin 加进受信网关（仅这个 feed 地址，不放松 SafeFetcher 的其它判定）
* 注册 `kind=wechat`、`adapter=wechat_feed`、`source_identity=wechat-mp-via-weread` 的来源
* 采集时只请求 feed；条目 URL 保持真实公众号原文，正文逐字取自 feed
* 来源标为**未核验**，每条候选都进人工复核，机器不会自动发布

## 三、命令行 / 源码方式（等价）

```bash
export CAMPUS_WECHAT_FEED_ENABLED=true
export CAMPUS_WECHAT_FEEDS="教务处=http://127.0.0.1:4000/feeds/all.json"
export CAMPUS_TRUSTED_GATEWAY_ORIGINS="http://127.0.0.1:4000"
python scripts/daily_update.py --data-dir data-operator --apply
```

## 四、四项风险（不许淡化）

1. **账号封控**：微信读书账号可能被限制（上游 README 自述添加频率过高会进「小黑屋」，
   等 24 小时解封）。用它就接受这个风险。
2. **第三方转发**：部分上游实现会把请求转发到第三方服务，存在数据出境与依赖风险。
3. **上游已归档**：WeWe RSS 已被作者归档（只读），接口随时可能失效。
4. **绕过平台常规访问方式**：这条路线本身属于绕过微信常规访问方式，需你自行判断并承担风险。

## 五、验证与排错

* 来源是否注册：产物日志「来源注册 新增 1」；或管理接口 `GET /api/v1/admin/sources`。
* `feed_https_required`：本机回环 feed 必须把 `http://127.0.0.1:端口` 放进
  `CAMPUS_TRUSTED_GATEWAY_ORIGINS`（桌面版 exe 会自动做）。
* `wechat_feed_disabled`：`CAMPUS_WECHAT_FEED_ENABLED` 没开。
* `wechat_feed_requires_fulltext`：feed 只有标题/摘要，打开上游全文模式。
* `feed_item_outside_wechat`：feed 里有非 `mp.weixin.qq.com` 的条目，先在上游过滤。
* 抓到的条目在复核队列里都是 pending：这是设计，未核验来源必须人工过。
