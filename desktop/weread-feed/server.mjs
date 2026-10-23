// 本机公众号 feed 服务：把 weread-omni 的「微信公众号订阅」导出成 JSON Feed。
// 全程本机：登录凭据在 ~/.config/weread/，不经过任何第三方中转。
// 端口默认 4011；偷闲通过 CAMPUS_WECHAT_FEEDS 读 /feeds/all.json。
//
// 设计：
//   * 请求永远立即返回缓存（feed.json），不阻塞；
//   * 启动时若缓存缺失或过旧，后台重建；之后每 WEREAD_FEED_MAX_AGE_MINUTES 分钟重建一次；
//   * 重建用本目录自带的 weread-omni，不依赖系统 node 包。

import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const feedFile = join(here, "feed.json");
const cli = join(here, "node_modules", "weread-omni", "dist", "cli.js");
const port = Number(process.env.WEREAD_FEED_PORT || 4011);
const maxAgeMs = Math.max(10, Number(process.env.WEREAD_FEED_MAX_AGE_MINUTES || 360)) * 60_000;

let building = false;
let lastError = "";

function log(message) {
  console.log(`[weread-feed] ${message}`);
}

function rebuild(reason) {
  if (building) return;
  if (!existsSync(cli)) {
    lastError = "missing weread-omni";
    log("跳过重建：" + lastError);
    return;
  }
  building = true;
  log(`重建 feed（${reason}）...`);
  const child = spawn(
    process.execPath,
    [cli, "public-accounts", "feed", "subscriptions", "--format", "json", "--limit", "100", "--refresh", "--out", feedFile],
    { cwd: here, stdio: "ignore", windowsHide: true },
  );
  child.on("exit", (code) => {
    building = false;
    if (code !== 0) {
      lastError = `cli exit ${code}`;
      log("重建失败：" + lastError);
    } else {
      lastError = "";
      log("重建完成");
    }
  });
}

if (!existsSync(feedFile)) {
  rebuild("首次启动，缓存为空");
} else if (Date.now() - statSync(feedFile).mtimeMs > maxAgeMs) {
  rebuild("缓存过期");
}
setInterval(() => rebuild("定时刷新"), maxAgeMs);

createServer((request, response) => {
  if (!request.url || !request.url.startsWith("/feeds/")) {
    response.writeHead(404, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ error: "not_found" }));
    return;
  }
  let body;
  try {
    body = existsSync(feedFile)
      ? readFileSync(feedFile)
      : Buffer.from(
          JSON.stringify(
            {
              version: "https://jsonfeed.org/version/1.1",
              title: "WeRead public-account subscriptions (warming up)",
              items: [],
              _status: building ? "building" : lastError || "empty",
            },
            null,
            2,
          ),
        );
  } catch (error) {
    response.writeHead(500, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ error: String(error) }));
    return;
  }
  response.writeHead(200, {
    "content-type": "application/feed+json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(body);
}).listen(port, "127.0.0.1", () => log(`serving http://127.0.0.1:${port}/feeds/all.json`));
