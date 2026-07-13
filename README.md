# ZUIG 订阅管理系统 (SubManager)

一套自托管的 **多订阅源合并管理面板**。把多台代理面板（S-UI-X 等）的订阅 URL 收拢到「源池」，再按需要组合成多个订阅「组合」，每个组合自动生成 `base64 / Clash / sing-box` 三种格式的订阅端点，并提供一个 Web 管理面板做增删改、状态查看、节点预览。

> 设计目标：换 VPS 或重装系统时，一条命令即可在新机器上拉起整套服务。

---

## 一、架构概览

```
                      ┌─────────────────────────────────────┐
   各 VPS 订阅 URL ──► │  源池 (sources)                     │
   (S-UI-X /sub/xxx)  │  tp / sp / jp / sb ... 各抓一次      │
                      └───────────────┬─────────────────────┘
                                      │ 引用
                      ┌───────────────┴─────────────────────┐
                      │  组合 (combos)                      │
                      │  mix = 全量 / mix1 = 前三 / mix2=SB  │
                      └───────────────┬─────────────────────┘
                                      │ convert.py 生成
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
   /mix (base64)               /mix/clash                  /mix/singbox
        └──────────────┬──────────────┬──────────────┬──────────────┘
                  Nginx 443 (反代)    │          zsm 服务 (127.0.0.1:8088)
                              /admin/ ┘   ◄── 管理面板 (server.py)
```

- **源池 (sources)**：只配一次的真实订阅入口；失效时保留上次节点继续服务。
- **组合 (combos)**：从源池挑选源的组合视图；每个组合独立生成三格式端点。
- **convert.py**：抓源 → 缓存 → 解析全部协议（hysteria2/vless/tuic/anytls/trojan/vmess/ss）→ 生成标准 Clash YAML + sing-box JSON + base64 拼接。
- **server.py**：独立 HTTP 服务（仅标准库，无 pip 依赖），提供 Web 面板 + API + 订阅预览代理；监听 `127.0.0.1:8088`，由 Nginx 反代 `/admin/`。
- **merge_sub.sh**：读取配置 → 调 convert.py 生成静态文件 → 生成 Nginx 多组合端点配置 → reload。

---

## 二、目录结构

```
SubManager/
├── install.sh                 # 一键部署脚本（目标 VPS 上运行）
├── server.py                  # 管理面板后端 + API（纯标准库）
├── convert.py                 # 订阅格式转换器（纯标准库）
├── merge_sub.sh               # Nginx 配置生成 + reload
├── README.md
├── .gitignore
├── deploy/
│   ├── zsm.service            # systemd 单元模板（读 .env）
│   ├── .env.example           # 部署变量示例（复制为 .env 填真实值，不入库）
│   └── sub_configs.json.example  # 配置示例（无真实订阅数据）
└── tools/
    ├── check_local.py         # 部署前本地语法校验（py + 内联 JS）
    └── push.py                # 开发用：推送代码到已部署 VPS（凭据读 tools/.env）
```

> **隐私说明**：仓库内**不包含任何真实订阅 URL、Token、服务器 IP、SSH 密码**。
> 真实配置只在你自己的服务器 `/opt/sub-converter/` 上，以及本地的 `deploy/.env`、`tools/.env`（均已被 `.gitignore` 忽略）。
> `sub_configs.json` 已加入忽略列表，请改用 `deploy/sub_configs.json.example`。

---

## 三、前置条件

目标 VPS 需满足：

- Linux（Debian / Ubuntu / CentOS 均可），已装 `systemd`
- `python3` >= 3.7（已在 Python 3.9 验证；**纯标准库，无需 pip 安装任何包**）
- `nginx` 已安装并可用（`nginx -t` / `nginx -s reload`）
- 一个已解析到该 VPS 的域名，以及对应的 SSL 证书（Let's Encrypt / acme.sh / 宝塔 均可）
- 根权限（部署脚本需要写 `/opt`、`/etc/systemd`、`nginx` 配置并重启服务）

---

## 四、一键部署（新 VPS / 重装）

1. 把仓库传到目标 VPS（或 `git clone`）：
   ```bash
   git clone git@github.com:zuig/submanager.git
   cd submanager
   ```

2. 运行安装脚本（需 root）：
   ```bash
   sudo bash install.sh
   ```
   脚本会逐项提示（均有默认值，直接回车即可，也可在命令前用环境变量非交互传入）：
   - **订阅域名** `SUB_DOMAIN`：如 `sub.example.com`
   - **SSL 证书 fullchain / privkey 路径**：脚本会尝试自动探测常见路径，探测不到请手动填
   - **Nginx 站点配置输出路径**：宝塔默认 `/www/server/panel/vhost/nginx/<域名>.conf`；标准 nginx 默认 `/etc/nginx/conf.d/<域名>.conf`
   - **管理密码** `ZSM_PASS`：首次部署自动随机生成并**打印在结尾**，请妥善保存

3. 部署完成后脚本会：
   - 创建 `/opt/sub-converter`，拷贝程序
   - 写入 `.env`（权限 600，含密码/域名/证书路径）
   - 注册并启动 `systemd` 服务 `zsm`
   - 生成 Nginx 配置并重载
   - 跑一次 `convert.py` 首次抓取
   - 打印面板地址与自检结果

4. 打开 `https://<你的域名>/admin/`，用打印的管理密码登录。

### 非交互部署示例

```bash
sudo SUB_DOMAIN=sub.example.com \
     NGINX_CERT_FULL=/etc/letsencrypt/live/sub.example.com/fullchain.pem \
     NGINX_CERT_KEY=/etc/letsencrypt/live/sub.example.com/privkey.pem \
     NGINX_CONF_PATH=/etc/nginx/conf.d/sub.example.com.conf \
     ZSM_PASS='your-strong-pass' \
     bash install.sh
```

---

## 五、配置你的真实订阅源

首次部署写入的是**示例** `sub_configs.json`（无真实节点）。两种填法：

**方式 A（推荐）：Web 面板添加**
1. 登录面板 → 「新增源」
2. 粘贴你的订阅 URL（如 `https://sui.xxx.example.com/sub/<token>`），名称/IP/所在地会自动提取
3. 在「组合」里勾选该源，保存即可生成对应端点

**方式 B：直接编辑配置**
编辑 `/opt/sub-converter/sub_configs.json`，按 `deploy/sub_configs.json.example` 的结构填 `sources[].url` 与 `combos[].sources`，然后：
```bash
cd /opt/sub-converter && python3 convert.py
bash /opt/sub-converter/merge_sub.sh
systemctl restart zsm
```

---

## 六、日常维护

| 操作 | 命令 |
|------|------|
| 查看服务状态 | `systemctl status zsm` |
| 重启服务 | `systemctl restart zsm` |
| 查看日志 | `journalctl -u zsm -n 50 -f` |
| 全部刷新订阅 | 面板点「全部更新」，或 `cd /opt/sub-converter && python3 convert.py` |
| 重新生成 Nginx | `bash /opt/sub-converter/merge_sub.sh` |
| 改管理密码 | 编辑 `/opt/sub-converter/.env` 的 `ZSM_PASS` 后 `systemctl restart zsm` |
| 改域名/证书 | 编辑 `.env` 对应项 → `bash /opt/sub-converter/merge_sub.sh` → `systemctl restart zsm` |

---

## 七、换新 VPS / 迁移步骤

1. 新 VPS 装好 nginx + python3 + 证书，解析域名
2. `git clone` 本仓库并 `sudo bash install.sh`（用新域名/证书路径）
3. 把**旧服务器** `/opt/sub-converter/sub_configs.json` 拷到新服务器同路径（含你的真实订阅源），或在新面板重新添加源
4. 如需保留会话无关；节点缓存会在首次 `convert.py` 时重新抓取

---

## 八、故障排查

- **面板打不开 / 404**：检查 `zsm` 是否 `active`；看 `journalctl -u zsm`；确认 Nginx 配置已 reload（`nginx -t`）。
- **订阅端点 0 节点**：源 URL 失效或被墙；面板「源池」里看各源 `status`，失败的源保留上次节点。
- **Nginx reload 失败**：证书路径不对；`nginx -t` 看具体报错，修正 `.env` 后重跑 `merge_sub.sh`。
- **改了 combo 端点 404**：combo 增删后 `merge_sub.sh` 会自动重生 Nginx 配置；若手动改配置需重跑它。
- **预览按钮 403**：预览只代理 `SUB_DOMAIN` 域名下的 URL（防 SSRF），其他域名会被拒。

---

## 九、安全提示

- 管理密码默认随机生成并仅打印一次，**重装不会丢失**（`.env` 会被保留）。
- 面板登录 Cookie 为 `HttpOnly` + `SameSite=Lax`，会话存内存（服务重启即失效）。
- 预览代理有域名白名单，禁止代理任意外部地址（防 SSRF）。
- **切勿**把 `/opt/sub-converter/.env`、`sub_configs.json`、本地 `tools/.env` 提交到公开仓库。
