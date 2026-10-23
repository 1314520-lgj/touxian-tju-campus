// 公众号登录：用微信扫码登录「微信读书」，凭据只存在本机 ~/.config/weread/。
// 用法：本目录 login.bat（或 node login.mjs）
// 扫完码会自动返回；之后重启「偷闲·天大版」即可拉取公众号文章。

import { AccountManager } from "weread-omni";
import QRCode from "qrcode";
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "wechat-login-qr.png");

console.log("");
console.log("偷闲·天大版 · 公众号登录");
console.log("----------------------------------------");
console.log("正在向微信申请登录码 ...");

const manager = new AccountManager();
try {
  const summary = await manager.login("default", {
    onQr: async (url, stage) => {
      await QRCode.toFile(out, url, { width: 560, margin: 2, errorCorrectionLevel: "M" });
      console.log("");
      console.log("二维码已保存：" + out);
      console.log("请用微信扫码（5 分钟内有效）。扫完在手机上点确认。");
      execFile("cmd", ["/c", "start", "", out]);
    },
    onStatus: (status) => {
      console.log("状态：" + status);
    },
    onOtp: async () => {
      throw new Error("需要手机验证码，请稍后重试或换用手机端确认");
    },
  });
  console.log("");
  console.log("登录成功：" + JSON.stringify(summary));
  console.log("现在可以关闭本窗口，双击桌面的「偷闲·天大版」。");
} catch (error) {
  console.log("");
  console.log("登录失败：" + (error && error.message ? error.message : String(error)));
  process.exitCode = 1;
}
