#!/bin/bash
# ==========================================
# ZUIG 订阅生成脚本 v3 —— 源池 + 多组合架构
# 读 sub_configs.json:
#   1) 调 convert.py 抓取全部源 + 生成各组合 (slug_sub.txt / slug_clash.yaml / slug_singbox.json)
#   2) 依据 combos 生成 Nginx 多组合端点配置
#   3) cp 到 vhost + nginx -t + reload
#
# 可经环境变量覆盖（install.sh 会自动注入，换新 VPS 时改这里或交互填入）：
#   SUB_DOMAIN        默认 sub.example.com
#   NGINX_CERT_FULL   默认 /etc/letsencrypt/live/example.com/fullchain.pem
#   NGINX_CERT_KEY    默认 /etc/letsencrypt/live/example.com/privkey.pem
#   NGINX_CONF_PATH   默认 /etc/nginx/conf.d/sub.example.com.conf
# ==========================================
set -uo pipefail

# 若 /opt/sub-converter/.env 存在则加载（install.sh 写入，含域名/证书路径/密码）
if [ -f /opt/sub-converter/.env ]; then
  set -a; . /opt/sub-converter/.env; set +a
fi

# ---- 从环境读取（带通用默认值，install.sh 部署时覆盖）----
SUB_DOMAIN="${SUB_DOMAIN:-sub.example.com}"
NGINX_CERT_FULL="${NGINX_CERT_FULL:-/etc/letsencrypt/live/example.com/fullchain.pem}"
NGINX_CERT_KEY="${NGINX_CERT_KEY:-/etc/letsencrypt/live/example.com/privkey.pem}"
NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/sub.example.com.conf}"

SCRIPT_DIR="/opt/sub-converter"
CONF_FILE="$SCRIPT_DIR/sub_configs.json"
OUTPUT_NGINX_TMP="$SCRIPT_DIR/.nginx_conf.tmp"
LOG="$SCRIPT_DIR/merge.log"
SUB_NAME="ZUIG VPS SUB"
PYTHON="/usr/bin/python3"; [ -x "$PYTHON" ] || PYTHON="python3"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始生成订阅 (多组合)..." | tee -a "$LOG"

if [ ! -f "$CONF_FILE" ]; then
    echo "ERROR: 配置文件 $CONF_FILE 不存在!" | tee -a "$LOG"
    exit 1
fi

# ==========================================
# 1. 生成各组合文件 (convert.py 读 sub_configs.json 并生成全部 combo)
# ==========================================
CONVERTER="$SCRIPT_DIR/convert.py"
if [ -f "$CONVERTER" ]; then
    echo "  [*] 运行格式转换器 (抓源 + 多组合生成)..." | tee -a "$LOG"
    CONV_OUT=$($PYTHON "$CONVERTER" 2>&1) || true
    echo "$CONV_OUT" | tee -a "$LOG"
else
    echo "  [!] 转换器 $CONVERTER 不存在!" | tee -a "$LOG"
    exit 1
fi

# ==========================================
# 2. 生成 Nginx 配置 (每个 combo 三个端点)
# ==========================================
# 把参数导出给内联 Python 使用
export SUB_DOMAIN NGINX_CERT_FULL NGINX_CERT_KEY
$PYTHON - <<'PYEOF' > "$OUTPUT_NGINX_TMP"
import os, json
DOMAIN   = os.environ.get('SUB_DOMAIN', 'sub.example.com')
CERT_FULL= os.environ.get('NGINX_CERT_FULL', '/etc/letsencrypt/live/example.com/fullchain.pem')
CERT_KEY = os.environ.get('NGINX_CERT_KEY',  '/etc/letsencrypt/live/example.com/privkey.pem')

CFG=json.load(open("/opt/sub-converter/sub_configs.json"))
COMBOS=CFG.get("combos",[])
SUB_NAME="ZUIG VPS SUB"
L=[]
L.append("server {")
L.append("    listen 443 ssl http2;")
L.append("    server_name %s;" % DOMAIN)
L.append("    ssl_certificate     %s;" % CERT_FULL)
L.append("    ssl_certificate_key %s;" % CERT_KEY)
L.append("    ssl_protocols TLSv1.2 TLSv1.3;")
L.append("    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;")
L.append("")
for c in COMBOS:
    slug=c["slug"]
    L.append("    # 组合 %s" % slug)
    L.append("    location = /%s {" % slug)
    L.append('        add_header Content-Type "text/plain; charset=utf-8";')
    L.append('        add_header Content-Disposition "attachment; filename=\\"%s.txt\\"";' % SUB_NAME)
    L.append("        alias /opt/sub-converter/%s_sub.txt;" % slug)
    L.append("    }")
    L.append("    location = /%s/clash {" % slug)
    L.append("        default_type text/yaml;")
    L.append('        add_header Content-Disposition "attachment; filename=\\"%s.yaml\\"";' % SUB_NAME)
    L.append("        alias /opt/sub-converter/%s_clash.yaml;" % slug)
    L.append("    }")
    L.append("    location = /%s/singbox {" % slug)
    L.append("        default_type application/json;")
    L.append('        add_header Content-Disposition "attachment; filename=\\"%s.json\\"";' % SUB_NAME)
    L.append("        alias /opt/sub-converter/%s_singbox.json;" % slug)
    L.append("    }")
    L.append("")
L.append("    # Web 管理面板 (保留 /admin 前缀透传, 后端路由基于 /admin/*)")
L.append("    location = /admin {")
L.append("        return 301 /admin/;")
L.append("    }")
L.append("    location /admin/ {")
L.append("        proxy_pass http://127.0.0.1:8088;")
L.append("        proxy_set_header Host $host;")
L.append("        proxy_set_header X-Real-IP $remote_addr;")
L.append("        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
L.append("        proxy_set_header X-Forwarded-Proto $scheme;")
L.append('        add_header Cache-Control "no-store, no-cache, must-revalidate" always;')
L.append('        add_header Pragma "no-cache" always;')
L.append("        expires -1;")
L.append("    }")
L.append("}")
open("/opt/sub-converter/.nginx_conf.tmp","w").write("\n".join(L)+"\n")
PYEOF

cp "$OUTPUT_NGINX_TMP" "$NGINX_CONF_PATH" && nginx -t 2>&1 && nginx -s reload 2>&1 && echo "  [=] Nginx 已重载" | tee -a "$LOG" || echo "  [!] Nginx 重载失败!" | tee -a "$LOG"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 生成完成！" | tee -a "$LOG"
