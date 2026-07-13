#!/usr/bin/env bash
# ============================================================
# ZUIG 订阅管理系统 —— 一键部署脚本 (install.sh)
# ------------------------------------------------------------
# 作用：在目标 VPS 上安装并启动整套订阅管理系统
#   - 创建 /opt/sub-converter
#   - 拷贝 server.py / convert.py / merge_sub.sh
#   - 仅在首次时写入 sub_configs.json（保留已有配置，便于重装/迁移）
#   - 生成 .env（管理密码 / 域名 / 证书路径）
#   - 注册 systemd 服务 zsm
#   - 生成 Nginx 站点配置并 reload
#   - 启动服务 + 首次抓取生成订阅
#
# 依赖（均为系统自带，无需 pip）：
#   python3 (>=3.7)  nginx  systemd
#
# 用法：
#   sudo bash install.sh
#   或带上默认值非交互：
#   sudo SUB_DOMAIN=sub.example.com \
#        NGINX_CERT_FULL=/etc/nginx/ssl/fullchain.pem \
#        NGINX_CERT_KEY=/etc/nginx/ssl/privkey.pem \
#        NGINX_CONF_PATH=/etc/nginx/conf.d/sub.example.com.conf \
#        ZSM_PASS='your-strong-pass' \
#        bash install.sh
# ============================================================
set -euo pipefail

# 脚本所在目录（仓库根）
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/sub-converter"

# ---------- 0. 权限 / 环境检查 ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "✗ 请使用 root 运行： sudo bash install.sh" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "✗ 未找到 python3，请先安装"; exit 1; }
command -v nginx   >/dev/null 2>&1 || { echo "✗ 未找到 nginx，请先安装"; exit 1; }

# ---------- 1. 部署参数（交互 / 环境变量） ----------
echo "============================================"
echo " ZUIG 订阅管理系统 —— 一键部署"
echo "============================================"

prompt() {  # prompt <变量名> <提示> <默认值>
  local var="$1" text="$2" def="$3" val
  if [ -n "${!var:-}" ]; then val="${!var}"; else
    read -r -p "$text [$def]: " val
    val="${val:-$def}"
  fi
  printf -v "$var" '%s' "$val"
}

# 域名（默认 sub.zuig.net，仅占位；请换成你自己的域名）
prompt SUB_DOMAIN     "订阅域名 (ServerName)"        "sub.zuig.net"

# 证书：自动探测常见路径
DEF_CERT_FULL="/www/server/panel/vhost/cert/${SUB_DOMAIN#*.}/fullchain.pem"
DEF_CERT_KEY="/www/server/panel/vhost/cert/${SUB_DOMAIN#*.}/privkey.pem"
[ -f "$DEF_CERT_FULL" ] || DEF_CERT_FULL="/etc/letsencrypt/live/${SUB_DOMAIN}/fullchain.pem"
[ -f "$DEF_CERT_KEY" ]  || DEF_CERT_KEY="/etc/letsencrypt/live/${SUB_DOMAIN}/privkey.pem"
prompt NGINX_CERT_FULL "SSL 证书 fullchain 路径"     "$DEF_CERT_FULL"
prompt NGINX_CERT_KEY  "SSL 证书 privkey 路径"       "$DEF_CERT_KEY"

if [ ! -f "$NGINX_CERT_FULL" ] || [ ! -f "$NGINX_CERT_KEY" ]; then
  echo "⚠ 证书文件不存在：$NGINX_CERT_FULL / $NGINX_CERT_KEY"
  echo "  请先通过 acme.sh / certbot / 宝塔 申请证书，再重跑本脚本。"
  exit 1
fi

# Nginx 配置落点：自动探测 宝塔 / 标准 nginx
if [ -d /www/server/panel/vhost/nginx ]; then
  DEF_NGINX_CONF="/www/server/panel/vhost/nginx/${SUB_DOMAIN}.conf"
elif [ -d /etc/nginx/conf.d ]; then
  DEF_NGINX_CONF="/etc/nginx/conf.d/${SUB_DOMAIN}.conf"
else
  DEF_NGINX_CONF="/etc/nginx/sites-enabled/${SUB_DOMAIN}.conf"
fi
prompt NGINX_CONF_PATH "Nginx 站点配置输出路径"      "$DEF_NGINX_CONF"

# 管理密码：优先 .env 已有值 → 传入 ZSM_PASS → 随机生成
EXISTING_PASS=""
[ -f "$INSTALL_DIR/.env" ] && EXISTING_PASS="$(grep '^ZSM_PASS=' "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' || true)"
if [ -n "$EXISTING_PASS" ]; then
  ZSM_PASS="$EXISTING_PASS"
  echo "• 沿用已有 .env 中的 ZSM_PASS（不覆盖）"
elif [ -n "${ZSM_PASS:-}" ]; then
  echo "• 使用传入的 ZSM_PASS"
else
  ZSM_PASS="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"
  echo "• 已随机生成管理密码：$ZSM_PASS   （请妥善保存，重装不会丢失）"
fi

# ---------- 2. 安装目录 + 拷贝程序 ----------
echo "• 安装到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/cache"
cp "$SRC_DIR/server.py"    "$INSTALL_DIR/server.py"
cp "$SRC_DIR/convert.py"   "$INSTALL_DIR/convert.py"
cp "$SRC_DIR/merge_sub.sh" "$INSTALL_DIR/merge_sub.sh"
chmod +x "$INSTALL_DIR/merge_sub.sh"

# 配置文件：仅首次写入，保留已有（重装/迁移不破坏真实订阅源）
if [ -f "$INSTALL_DIR/sub_configs.json" ]; then
  echo "• 检测到已有 sub_configs.json，保留不覆盖（如需重置请手动删除）"
else
  echo "• 写入示例 sub_configs.json（请到面板或编辑文件填入你的真实订阅源）"
  cp "$SRC_DIR/deploy/sub_configs.json.example" "$INSTALL_DIR/sub_configs.json"
fi

# ---------- 3. 写入 .env（供 systemd 与 merge_sub.sh 读取） ----------
cat > "$INSTALL_DIR/.env" <<EOF
# 由 install.sh 生成，请勿提交到公开仓库
ZSM_PASS=$ZSM_PASS
SUB_DOMAIN=$SUB_DOMAIN
NGINX_CERT_FULL=$NGINX_CERT_FULL
NGINX_CERT_KEY=$NGINX_CERT_KEY
NGINX_CONF_PATH=$NGINX_CONF_PATH
EOF
chmod 600 "$INSTALL_DIR/.env"
echo "• 已写入 $INSTALL_DIR/.env (权限 600)"

# ---------- 4. systemd 服务 ----------
echo "• 注册 systemd 服务 zsm"
cp "$SRC_DIR/deploy/zsm.service" /etc/systemd/system/zsm.service
systemctl daemon-reload
systemctl enable zsm

# ---------- 5. 生成并加载 Nginx 配置 ----------
echo "• 生成 Nginx 配置 -> $NGINX_CONF_PATH"
export SUB_DOMAIN NGINX_CERT_FULL NGINX_CERT_KEY NGINX_CONF_PATH
bash "$INSTALL_DIR/merge_sub.sh" >/dev/null 2>&1 || {
  echo "⚠ merge_sub.sh 执行异常，请检查 Nginx 证书路径后手动运行："
  echo "  SUB_DOMAIN=$SUB_DOMAIN NGINX_CERT_FULL=$NGINX_CERT_FULL NGINX_CERT_KEY=$NGINX_CERT_KEY NGINX_CONF_PATH=$NGINX_CONF_PATH bash $INSTALL_DIR/merge_sub.sh"
}

# ---------- 6. 启动服务 + 首次生成 ----------
echo "• 启动 zsm 服务"
systemctl restart zsm
sleep 2
if [ "$(systemctl is-active zsm)" != "active" ]; then
  echo "✗ zsm 启动失败，查看日志： journalctl -u zsm -n 50"
  exit 1
fi

echo "• 首次抓取生成订阅（convert.py）"
cd "$INSTALL_DIR" && python3 convert.py 2>&1 | tail -8 || true

# ---------- 7. 自检 ----------
echo "• 自检端点"
curl -s -k -o /dev/null -w "  /admin/      -> HTTP %{http_code}\n" "https://127.0.0.1/admin/" 2>/dev/null || true
for s in mix mix1 mix2; do
  curl -s -k -o /dev/null -w "  /$s          -> HTTP %{http_code}\n" "https://127.0.0.1/$s" 2>/dev/null || true
done

echo "============================================"
echo " 部署完成！"
echo " 面板:  https://$SUB_DOMAIN/admin/"
echo " 管理密码: $ZSM_PASS"
echo " 首次部署请到面板「新增源」填入你的真实订阅 URL"
echo "============================================"
