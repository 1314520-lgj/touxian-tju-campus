#!/usr/bin/env bash
# 偷闲·天大版 —— 一键部署脚本（Alibaba Cloud Linux 3 / 2 GiB 小内存版）
#
# 用法：
#   bash deploy.sh <你的域名> <管理员邮箱> <SMTP主机> <SMTP发件人>
# 例：
#   bash deploy.sh campus.example.com me@qq.com smtp.qq.com me@qq.com
#
# 脚本会：生成 3 个随机密钥 → 写 .env → 构建 → 启动 → 自检
# 交互式输入的项：SMTP 密码（授权码），不会回显、不写进任何日志

set -euo pipefail

if [ $# -lt 4 ]; then
  cat <<'USAGE'
用法：bash deploy.sh <域名> <管理员邮箱> <SMTP主机> <SMTP发件人>

例：bash deploy.sh campus.example.com me@qq.com smtp.qq.com me@qq.com

参数说明：
  域名          已解析到本机公网 IP 的域名（香港节点无需备案）
  管理员邮箱    你用来登录的邮箱，必须能收信
  SMTP主机      例：smtp.qq.com / smtp.163.com / smtp.gmail.com
  SMTP发件人    通常与管理员邮箱相同
USAGE
  exit 1
fi

DOMAIN="$1"
ADMIN_EMAIL="$2"
SMTP_HOST="$3"
SMTP_FROM="$4"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "=============================================="
echo " 偷闲·天大版 —— 一键部署"
echo " 域名      : $DOMAIN"
echo " 管理员    : $ADMIN_EMAIL"
echo " SMTP      : $SMTP_HOST"
echo " 目录      : $HERE"
echo "=============================================="
echo

# ---------- 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "[失败] 未安装 Docker，请先运行 preflight.sh 查看安装命令"; exit 1; }
docker info >/dev/null 2>&1 || { echo "[失败] Docker daemon 未运行：systemctl start docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "[失败] 缺少 docker compose v2"; exit 1; }

if [ -f .env ]; then
  echo "[注意] .env 已存在。"
  read -r -p "       覆盖它吗？原有配置会丢失 (yes/N): " ans
  [ "$ans" = "yes" ] || { echo "已取消。"; exit 0; }
  cp .env ".env.bak.$(date +%s)"
  echo "       已备份原 .env"
fi

# ---------- 生成密钥 ----------
echo
echo "[1/5] 生成密钥..."
PG_PASS="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
APP_SECRET="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "      PostgreSQL 密码、应用密钥已生成（各 64 字符）"

# ---------- SMTP 密码 ----------
echo
echo "[2/5] 配置 SMTP"
echo "      请输入 SMTP 授权码（不是邮箱登录密码；输入时不显示）"
read -r -s -p "      授权码: " SMTP_PASS
echo
if [ -z "$SMTP_PASS" ]; then
  echo "[失败] 授权码不能为空——生产模式没有 SMTP 就无法登录"
  exit 1
fi

# 端口推断
SMTP_PORT=587
SMTP_SSL=false
SMTP_STARTTLS=true
case "$SMTP_HOST" in
  smtp.qq.com|smtp.163.com|smtp.126.com) SMTP_PORT=465; SMTP_SSL=true; SMTP_STARTTLS=false ;;
  *) ;;
esac

# ---------- 写 .env ----------
echo
echo "[3/5] 写入 .env（权限 600）"
cat > .env <<EOF
# 由 deploy.sh 于 $(date '+%Y-%m-%d %H:%M:%S') 生成
CAMPUS_POSTGRES_PASSWORD=${PG_PASS}
CAMPUS_SECRET=${APP_SECRET}
CAMPUS_ALLOWED_ORIGINS=https://${DOMAIN}
CAMPUS_ADMIN_EMAILS=${ADMIN_EMAIL}
CAMPUS_SMTP_HOST=${SMTP_HOST}
CAMPUS_SMTP_PORT=${SMTP_PORT}
CAMPUS_SMTP_FROM=${SMTP_FROM}
CAMPUS_SMTP_USERNAME=${SMTP_FROM}
CAMPUS_SMTP_PASSWORD=${SMTP_PASS}
CAMPUS_SMTP_STARTTLS=${SMTP_STARTTLS}
CAMPUS_SMTP_SSL=${SMTP_SSL}
CAMPUS_FORWARDED_ALLOW_IPS=127.0.0.1
EOF
chmod 600 .env
echo "      已写入。注意：这个文件含明文密码，绝不提交、不外发。"

# ---------- 构建 ----------
echo
echo "[4/5] 构建镜像（约 5–10 分钟，请耐心等待）"
docker compose -f compose.small.yml config --quiet || { echo "[失败] compose 配置有误"; exit 1; }
docker compose -f compose.small.yml build api

# ---------- 启动 ----------
echo
echo "[5/5] 启动服务"
docker compose -f compose.small.yml up -d postgres
echo "      等待 PostgreSQL 就绪..."
for i in $(seq 1 30); do
  if docker compose -f compose.small.yml exec -T postgres pg_isready -U campus -d campus >/dev/null 2>&1; then
    echo "      PostgreSQL 就绪"
    break
  fi
  sleep 2
  [ "$i" = "30" ] && { echo "[失败] PostgreSQL 启动超时，查看：docker compose -f compose.small.yml logs postgres"; exit 1; }
done

docker compose -f compose.small.yml up -d api worker
sleep 5
docker compose -f compose.small.yml ps

# ---------- 自检 ----------
echo
echo "=============================================="
echo " 自检"
echo "=============================================="
if curl -fsS --max-time 10 http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
  echo "[通过] API 健康检查"
else
  echo "[失败] API 未响应。查看日志："
  echo "       docker compose -f compose.small.yml logs --tail=50 api"
  exit 1
fi

echo
echo "服务已启动，但**还没有完成验收**。必须再做两件事："
echo
echo "  1) 配置 HTTPS 反向代理（否则 https://${DOMAIN} 打不开）"
echo "     用 Caddy 最省事："
echo "       dnf install -y caddy || apt install -y caddy"
echo "       cat > /etc/caddy/Caddyfile <<'CADDY'"
echo "       ${DOMAIN} {"
echo "           reverse_proxy 127.0.0.1:8000"
echo "       }"
echo "       CADDY"
echo "       systemctl reload caddy"
echo
echo "  2) 真的收一封验证码邮件"
echo "     浏览器打开 https://${DOMAIN}/"
echo "     填入 ${ADMIN_EMAIL} → 点获取验证码 → 去邮箱收信"
echo
echo "     ⚠️ 收不到邮件就是 SMTP 没配对，命令排查："
echo "        docker compose -f compose.small.yml logs --tail=100 api | grep -i -E 'smtp|mail|login'"
echo
echo "只有「收到邮件并登录成功」才算部署完成。"
echo
echo "采集真实通知："
echo "  docker compose -f compose.small.yml exec -T api \\"
echo "    python scripts/daily_update.py --data-dir /var/lib/touxian --apply"
echo
echo "配置每天自动采集（crontab -e 加入）："
echo "  30 7 * * * cd ${HERE} && docker compose -f compose.small.yml exec -T api python scripts/daily_update.py --data-dir /var/lib/touxian --apply >> /var/log/touxian-daily.log 2>&1"
