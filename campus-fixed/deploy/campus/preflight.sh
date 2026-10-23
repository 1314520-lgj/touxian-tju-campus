#!/usr/bin/env bash
# 偷闲·天大版 —— 服务器环境预检
# 在云服务器上运行：bash preflight.sh
# 目的：在花时间部署之前，先确认这台机器到底能不能跑、能不能采到数据。

set -u
ok=0; warn=0; bad=0

say()  { printf '%-8s %s\n' "$1" "$2"; }
pass() { say "[通过]" "$1"; ok=$((ok+1)); }
note() { say "[注意]" "$1"; warn=$((warn+1)); }
fail() { say "[失败]" "$1"; bad=$((bad+1)); }

echo "=============================================="
echo " 偷闲·天大版 —— 服务器环境预检"
echo " 时间：$(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================================="
echo

# ---------- 1. 系统与资源 ----------
echo "── 1. 系统与资源 ──"
if [ -r /etc/os-release ]; then
  . /etc/os-release
  pass "系统：${PRETTY_NAME:-未知}"
else
  note "无法读取 /etc/os-release"
fi

cpus=$(nproc 2>/dev/null || echo 0)
mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
avail_mb=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
disk_gb=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')

echo "         CPU ${cpus} 核 | 内存 ${mem_mb} MB（可用 ${avail_mb} MB）| 磁盘可用 ${disk_gb} GB"

if [ "$mem_mb" -lt 1800 ]; then
  fail "内存不足 2 GB，跑不动 PostgreSQL + API + worker"
elif [ "$mem_mb" -lt 3800 ]; then
  note "内存 ${mem_mb} MB 偏紧：建议去掉 Meilisearch（本地检索可兜底），并加 2 GB swap"
else
  pass "内存充足（${mem_mb} MB），可跑完整四件套"
fi

if [ "${disk_gb:-0}" -lt 15 ]; then
  note "磁盘可用仅 ${disk_gb} GB，快照会持续增长，建议扩容"
else
  pass "磁盘充足（${disk_gb} GB 可用）"
fi

if [ "$cpus" -lt 2 ]; then
  note "CPU 仅 ${cpus} 核，构建镜像会较慢"
fi

# swap
swap_mb=$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$swap_mb" -lt 1024 ] && [ "$mem_mb" -lt 3800 ]; then
  note "swap 仅 ${swap_mb} MB。小内存机器建议：fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile"
else
  pass "swap ${swap_mb} MB"
fi
echo

# ---------- 2. Docker ----------
echo "── 2. 容器环境 ──"
if command -v docker >/dev/null 2>&1; then
  dv=$(docker --version 2>/dev/null)
  pass "Docker：$dv"
  if docker info >/dev/null 2>&1; then
    pass "Docker daemon 运行中"
  else
    fail "Docker 已安装但 daemon 未运行：systemctl start docker"
  fi
  if docker compose version >/dev/null 2>&1; then
    pass "Compose：$(docker compose version --short 2>/dev/null)"
  else
    fail "缺少 Compose v2：dnf install -y docker-compose-plugin 或参考官方文档"
  fi
else
  fail "未安装 Docker。Alibaba Cloud Linux 3 可执行：
         dnf install -y dnf-utils
         dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
         dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
         systemctl enable --now docker"
fi
echo

# ---------- 3. 网络：能否访问天大 ----------
echo "── 3. 关键网络可达性（决定能不能采集）──"
TJU_IP="202.113.2.198"
all_ok=1
for host in www.tju.edu.cn oaa.tju.edu.cn eee.tju.edu.cn; do
  out=$(curl -s -o /dev/null -w "%{http_code} %{time_total}" --max-time 15 \
        "https://${host}/" --resolve "${host}:443:${TJU_IP}" 2>/dev/null)
  code=$(echo "$out" | awk '{print $1}')
  secs=$(echo "$out" | awk '{print $2}')
  if [ "$code" = "200" ] || [ "$code" = "301" ] || [ "$code" = "302" ]; then
    pass "${host} 可达（HTTP ${code}，${secs}s）"
  else
    fail "${host} 不可达（结果：${out:-超时}）"
    all_ok=0
  fi
done

if [ "$all_ok" = "0" ]; then
  echo
  echo "         ⚠️ 这台机器采不到天大官网。可选方案："
  echo "            1) 换国内节点（教育网出口通常放行）"
  echo "            2) 用运营中继：在能访问校园网的机器上跑 deploy/operator/relay.py"
  echo "               逐字取证后，把 feed 交给这台服务器采集"
fi
echo

# ---------- 4. 出网：拉取依赖 ----------
echo "── 4. 出网能力（构建镜像需要）──"
for url in https://registry-1.docker.io/v2/ https://pypi.org/simple/ https://registry.npmmirror.com/; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 "$url" 2>/dev/null)
  if [ "$code" = "200" ] || [ "$code" = "401" ] || [ "$code" = "301" ]; then
    pass "${url} 可达（${code}）"
  else
    note "${url} 返回 ${code:-超时}（可能需要配置镜像加速）"
  fi
done
echo

# ---------- 5. 端口占用 ----------
echo "── 5. 端口占用 ──"
for p in 80 443 8000; do
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
      note "端口 ${p} 已被占用（若是 nginx/caddy 正常）"
    else
      pass "端口 ${p} 空闲"
    fi
  fi
done
echo

# ---------- 6. 域名与 HTTPS ----------
echo "── 6. 域名与 HTTPS（生产模式硬性要求）──"
echo "         生产模式要求：CAMPUS_ALLOWED_ORIGINS 必须是明确的 HTTPS 地址。"
echo "         直接用 IP 不行——证书签不了 IP。你需要一个域名解析到本机。"
if [ -n "${DOMAIN:-}" ]; then
  note "已通过环境变量指定 DOMAIN=${DOMAIN}，请确认已解析到本机公网 IP"
else
  note "未提供域名。部署前请准备一个（香港节点无需备案）"
fi
echo

# ---------- 汇总 ----------
echo "=============================================="
printf ' 通过 %d 项 | 注意 %d 项 | 失败 %d 项\n' "$ok" "$warn" "$bad"
echo "=============================================="
if [ "$bad" -gt 0 ]; then
  echo
  echo "有失败项。请先解决上面的 [失败]，再开始部署。"
  exit 1
fi
if [ "$all_ok" = "0" ]; then
  echo
  echo "环境本身可用，但这台机器采不到天大官网——"
  echo "要么换节点，要么走中继通路（见 deploy/operator/OPERATIONS.md）。"
  exit 2
fi
echo
echo "环境检查通过，可以开始部署。下一步："
echo "  1) 准备域名并解析到本机"
echo "  2) cd campus-fixed/deploy/campus && cp .env.example .env"
echo "  3) 填写 .env 的 6 个必填项（见部署指引）"
echo "  4) docker compose build api && docker compose up -d"
exit 0
