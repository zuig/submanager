#!/usr/bin/env python3
"""
ZUIG 订阅格式转换器 v2 —— 源池 + 多组合架构
============================================
存储: /opt/sub-converter/sub_configs.json   (sources 源池 + combos 组合，唯一真相)
缓存: /opt/sub-converter/cache/<source_id>.txt  (每个源抓取后的原始节点 URI，逐行)

设计要点:
- 抓取与生成解耦: 源(url)->缓存(原始URI) 与 组合(引用源)->三格式文件 分开
- 更新是源级: 单源更新 = 重抓该源 + 重生成引用它的组合; 不碰其他源
- 失败降级: 抓取失败保留上次缓存, 源标 status=fail, 组合继续用旧节点
- 内容哈希: 源/组合都记 content_hash, 只在内容真正变化时更新时间戳 / 重写文件
- 协议无关: 新增协议只要 URI scheme 能被 parse_uri 解析即可
"""
import base64, hashlib, json, os, re, subprocess, sys, time, urllib.parse

CONFIG_FILE = '/opt/sub-converter/sub_configs.json'
CACHE_DIR = '/opt/sub-converter/cache'
OUT_DIR = '/opt/sub-converter'
SUB_NAME = 'ZUIG VPS SUB'

# 部署相关（可通过环境变量覆盖，避免把域名/证书路径硬编码进仓库）
# install.sh 部署时会注入这些值；未设置时使用原 zuig 部署的默认值（仅占位，换新 VPS 请覆盖）
DOMAIN = os.environ.get('SUB_DOMAIN', 'sub.zuig.net')
NGINX_CERT_FULL = os.environ.get('NGINX_CERT_FULL', '/www/server/panel/vhost/cert/zuig.net/fullchain.pem')
NGINX_CERT_KEY = os.environ.get('NGINX_CERT_KEY', '/www/server/panel/vhost/cert/zuig.net/privkey.pem')
NGINX_CONF_PATH = os.environ.get('NGINX_CONF_PATH', '/www/server/panel/vhost/nginx/sub.zuig.net.conf')


# ──── 配置读写 ────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {'version': 2, 'sources': [], 'combos': []}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def find_source(cfg, sid):
    for s in cfg.get('sources', []):
        if s['id'] == sid:
            return s
    return None


def find_combo(cfg, slug):
    for c in cfg.get('combos', []):
        if c['slug'] == slug:
            return c
    return None


# ──── 缓存 ────
def cache_path(sid):
    return os.path.join(CACHE_DIR, f'{sid}.txt')


def read_cache(sid):
    p = cache_path(sid)
    if not os.path.exists(p):
        return []
    with open(p, 'r', encoding='utf-8') as f:
        return [ln.strip() for ln in f if ln.strip()]


def write_cache(sid, uris):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(sid), 'w', encoding='utf-8') as f:
        f.write('\n'.join(uris) + ('\n' if uris else ''))


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# ──── 抓取单个源 ────
def fetch_source(url, timeout=30):
    """返回 (ok, uris, error)。ok=False 时 uris=[]。"""
    try:
        r = subprocess.run(
            ['curl', '-s', '-k', '--connect-timeout', '10', '--max-time', str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 15
        )
    except Exception as e:
        return False, [], f'curl 异常: {e}'
    raw = (r.stdout or '').strip()
    if not raw:
        return False, [], '空响应 (HTTP 错误或网络不可达)'
    try:
        decoded = base64.b64decode(raw).decode('utf-8', errors='replace')
    except Exception as e:
        return False, [], f'base64 解码失败: {e}'
    uris = [ln.strip() for ln in decoded.strip().splitlines() if ln.strip() and '://' in ln]
    if not uris:
        return False, [], '解码后无有效节点 URI'
    return True, uris, None


def refresh_source(src, timeout=30):
    """
    抓单个源并写缓存, 更新元数据。
    返回 dict: {id, ok, changed, error, node_count}
    changed=True 表示节点内容相对上次有变化。
    """
    ok, uris, err = fetch_source(src['url'], timeout)
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    res = {'id': src['id'], 'ok': ok, 'changed': False, 'error': err, 'node_count': len(uris)}
    if not ok:
        # 失败降级: 保留上次缓存, 标 fail
        src['status'] = 'fail'
        src['last_fetch'] = now
        src['error'] = err
        # node_count 维持上次
        res['node_count'] = src.get('node_count', 0)
        return res
    new_hash = sha('\n'.join(uris))
    old_hash = src.get('content_hash', '')
    res['changed'] = (new_hash != old_hash)
    write_cache(src['id'], uris)
    src['status'] = 'ok'
    src['last_fetch'] = now
    src['error'] = None
    src['content_hash'] = new_hash
    src['node_count'] = len(uris)
    if res['changed']:
        src['last_change'] = now
    return res


# ──── 解析单个 URI ────
def parse_uri(uri):
    uri = uri.strip()
    if '://' not in uri:
        return None
    scheme, rest = uri.split('://', 1)
    scheme = scheme.lower()
    name = ''
    if '#' in rest:
        rest, name = rest.rsplit('#', 1)
        name = urllib.parse.unquote(name)
    params = {}
    if '?' in rest:
        rest, qs = rest.rsplit('?', 1)
        for pair in qs.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k] = urllib.parse.unquote(v)
    userinfo, hostport = rest.rsplit('@', 1) if '@' in rest else ('', rest)
    server = ''
    port = ''
    if hostport.startswith('['):
        bracket_end = hostport.find(']')
        if bracket_end >= 0:
            server = hostport[:bracket_end + 1]
            remainder = hostport[bracket_end + 1:]
            if remainder.startswith(':'):
                port = remainder[1:]
    else:
        if ':' in hostport:
            parts = hostport.rsplit(':', 1)
            server = parts[0]
            port = parts[1]
        else:
            server = hostport
    return {
        'scheme': scheme, 'name': name or f'{scheme}-{server}:{port}',
        'server': server, 'port': port, 'params': params,
        'userinfo': userinfo, 'raw_uri': uri,
    }


# ──── Clash 生成 ────
def uri_to_clash_proxy(p):
    s = p['scheme']; name = p['name']; srv = p['server']; port = p['port']; pm = p['params']
    if s == 'hysteria2':
        return {'name': name, 'type': 'hysteria2', 'server': srv, 'port': int(port),
                'password': p['userinfo'], 'sni': pm.get('sni', srv),
                'skip-cert-verify': pm.get('insecure', '0') == '1'}
    elif s == 'vless':
        sec = pm.get('security', '')
        proxy = {'name': name, 'type': 'vless', 'server': srv, 'port': int(port),
                 'uuid': p['userinfo'], 'tls': sec in ('reality', 'tls'),
                 'network': pm.get('type', 'tcp'), 'flow': pm.get('flow', '')}
        if sec == 'reality':
            proxy['servername'] = pm.get('sni', '')
            proxy['client-fingerprint'] = pm.get('fp', pm.get('fingerprint', 'chrome'))
            proxy['reality-opts'] = {'public-key': pm.get('pbk', ''), 'short-id': pm.get('sid', '')}
        elif sec == 'tls':
            proxy['servername'] = pm.get('sni', srv)
            if 'fp' in pm or 'fingerprint' in pm:
                proxy['client-fingerprint'] = pm.get('fp', pm.get('fingerprint'))
        return proxy
    elif s == 'tuic':
        parts = p['userinfo'].split(':', 1)
        return {'name': name, 'type': 'tuic', 'server': srv, 'port': int(port),
                'uuid': parts[0] if len(parts) > 1 else p['userinfo'],
                'password': parts[1] if len(parts) > 1 else p['userinfo'],
                'sni': pm.get('sni', srv),
                'alpn': [a.strip() for a in pm.get('alpn', 'h3,h2,http/1.1').split(',')],
                'congestion-controller': pm.get('congestion_control', 'bbr')}
    elif s == 'anytls':
        return {'name': name, 'type': 'anytls', 'server': srv, 'port': int(port),
                'password': p['userinfo'], 'sni': pm.get('sni', srv)}
    elif s == 'trojan':
        return {'name': name, 'type': 'trojan', 'server': srv, 'port': int(port),
                'password': p['userinfo'], 'sni': pm.get('sni', srv),
                'skip-cert-verify': pm.get('insecure', '0') == '1'}
    elif s == 'vmess':
        import base64 as b64
        try:
            j = json.loads(b64.urlsafe_b64decode(p['userinfo'] + '==').decode())
        except Exception:
            return None
        proxy = {'name': name, 'type': 'vmess', 'server': j.get('add', srv),
                 'port': int(j.get('port', port)), 'cipher': j.get('scy', 'auto'),
                 'uuid': j.get('id', ''), 'network': j.get('net', 'tcp'), 'tls': j.get('tls', '')}
        if j.get('net') == 'ws':
            proxy['ws-opts'] = {'path': j.get('path', '/'), 'headers': {'Host': j.get('host', '')}}
        elif j.get('net') == 'grpc':
            proxy['grpc-opts'] = {'grpc-service-name': j.get('path', '')}
        if j.get('tls') in ('tls', ''):
            proxy['servername'] = j.get('sni', '')
            if 'sni' in j:
                proxy['sni'] = j['sni']
        return proxy
    elif s in ('ss', 'shadowsocks'):
        method, ss_pw = 'aes-256-gcm', p['userinfo']
        if '@' not in p['userinfo']:
            try:
                dec = b64.urlsafe_b64decode(p['userinfo'] + '==').decode()
                method, ss_pw = dec.split(':', 1)
            except Exception:
                pass
        else:
            c = p['userinfo'].split(':', 1)
            if len(c) == 2:
                method, ss_pw = c
        return {'name': name, 'type': 'ss', 'server': srv, 'port': int(port),
                'cipher': method, 'password': ss_pw}
    else:
        return None


def generate_clash(uris):
    proxies = []
    names_seen = set()
    for u in uris:
        p = parse_uri(u)
        if not p:
            continue
        cp = uri_to_clash_proxy(p)
        if cp is None:
            continue
        tag = cp['name']
        if tag in names_seen:
            continue
        names_seen.add(tag)
        proxies.append(cp)
    yaml_lines = [
        'mixed-port: 7890', 'allow-lan: false', 'mode: rule', 'log-level: info',
        'external-controller: 127.0.0.1:9090', '', 'proxies:',
    ]
    for px in proxies:
        y = f"  - name: {px['name']}\n"
        y += f"    type: {px['type']}\n"
        y += f"    server: {px['server']}\n"
        y += f"    port: {px['port']}\n"
        for k, v in px.items():
            if k in ('name', 'type', 'server', 'port'):
                continue
            if isinstance(v, str):
                if v == '' or v is None:
                    continue
                v_esc = v.replace("'", "''")
                y += f"    {k}: '{v_esc}'\n"
            elif isinstance(v, list):
                if v:
                    y += f"    {k}: {json.dumps(v)}\n"
            else:
                y += f"    {k}: {v}\n"
        yaml_lines.append(y)
    group_names = [p['name'] for p in proxies]
    yaml_lines.extend([
        '', 'proxy-groups:', '  - name: AUTO', '    type: url-test', '    proxies:',
    ] + [f"      - '{n}'" for n in group_names] + [
        '    url: http://www.gstatic.com/generate_204', '    interval: 300', '',
        'rules:', '  - GEOIP,CN,DIRECT', '  - MATCH,AUTO', '',
    ])
    return '\n'.join(yaml_lines), len(proxies)


# ──── sing-box 生成 ────
def uri_to_singbox_outbound(p):
    s = p['scheme']; name = p['name']; srv = p['server']; port = p['port']; pm = p['params']
    ob = {'tag': name, 'type': s}
    if s == 'hysteria2':
        ob['server'] = srv; ob['server_port'] = int(port); ob['password'] = p['userinfo']
        ob['tls'] = {'enabled': True, 'server_name': pm.get('sni', srv)}
        ob['up'] = f"{int(pm.get('up_mbps', '100'))}Mbps"
        ob['down'] = f"{int(pm.get('down_mbps', '100'))}Mbps"
    elif s == 'vless':
        ob['server'] = srv; ob['server_port'] = int(port); ob['uuid'] = p['userinfo']
        flow = pm.get('flow', '')
        if flow:
            ob['flow'] = flow
        transport = {}
        net_type = pm.get('type', 'tcp')
        if net_type == 'ws':
            transport['type'] = 'ws'
            path = pm.get('path', '/')
            if path and path != '/':
                transport['path'] = path
            ws_host = pm.get('host', '')
            if ws_host:
                transport['headers'] = {'Host': ws_host}
        elif net_type == 'grpc':
            transport['type'] = 'grpc'
            gpath = pm.get('path', '')
            if gpath:
                transport['service_name'] = gpath
        if transport:
            ob['transport'] = transport
        sec = pm.get('security', '')
        if sec == 'reality':
            ob['tls'] = {'enabled': True, 'server_name': pm.get('sni', ''),
                         'reality': {'enabled': True, 'public_key': pm.get('pbk', ''),
                                     'short_id': pm.get('sid', '')},
                         'utls': {'enabled': True, 'fingerprint': pm.get('fp', pm.get('fingerprint', 'chrome'))}}
        elif sec == 'tls':
            sni = pm.get('sni', srv)
            tls_cfg = {'enabled': True, 'server_name': sni}
            if 'fp' in pm or 'fingerprint' in pm:
                tls_cfg['utls'] = {'enabled': True, 'fingerprint': pm.get('fp', pm.get('fingerprint', 'chrome'))}
            ob['tls'] = tls_cfg
    elif s == 'tuic':
        parts = p['userinfo'].split(':', 1)
        ob['server'] = srv; ob['server_port'] = int(port)
        ob['uuid'] = parts[0] if len(parts) > 1 else p['userinfo']
        ob['password'] = parts[1] if len(parts) > 1 else p['userinfo']
        ob['tls'] = {'enabled': True, 'server_name': pm.get('sni', srv)}
        alpn_list = [a.strip() for a in pm.get('alpn', 'h3,h2,http/1.1').split(',') if a.strip()]
        if alpn_list:
            ob['tls']['alpn'] = alpn_list
        cc = pm.get('congestion_control', 'bbr')
        if cc:
            ob['congestion_controller'] = cc
    elif s == 'anytls':
        ob['server'] = srv; ob['server_port'] = int(port); ob['password'] = p['userinfo']
        ob['tls'] = {'enabled': True, 'server_name': pm.get('sni', srv)}
    elif s == 'trojan':
        ob['server'] = srv; ob['server_port'] = int(port); ob['password'] = p['userinfo']
        ob['tls'] = {'enabled': True, 'server_name': pm.get('sni', srv)}
    elif s == 'vmess':
        import base64 as b64
        try:
            j = json.loads(b64.urlsafe_b64decode(p['userinfo'] + '==').decode())
        except Exception:
            j = {}
        ob['server'] = j.get('add', srv); ob['server_port'] = int(j.get('port', port))
        ob['uuid'] = j.get('id', ''); ob['security'] = j.get('scy', 'auto')
        ob['alter_id'] = int(j.get('aid', 0))
        transport = {}
        net = j.get('net', 'tcp')
        if net == 'ws':
            transport['type'] = 'ws'
            tp = {'path': j.get('path', '/')}
            h = j.get('host', '')
            if h:
                tp['headers'] = {'Host': h}
            transport.update(tp)
        elif net == 'grpc':
            transport['type'] = 'grpc'
            gp = j.get('path', '')
            if gp:
                transport['service_name'] = gp
        if transport:
            ob['transport'] = transport
        if j.get('tls') == 'tls':
            ob['tls'] = {'enabled': True, 'server_name': j.get('sni', '')}
    elif s in ('ss', 'shadowsocks'):
        method, ss_pw = 'aes-256-gcm', p['userinfo']
        if '@' not in p['userinfo']:
            try:
                dec = b64.urlsafe_b64decode(p['userinfo'] + '==').decode()
                method, ss_pw = dec.split(':', 1)
            except Exception:
                pass
        else:
            c = p['userinfo'].split(':', 1)
            if len(c) == 2:
                method, ss_pw = c
        ob['server'] = srv; ob['server_port'] = int(port)
        ob['method'] = method; ob['password'] = ss_pw
    else:
        return None
    return ob


def generate_singbox(uris):
    outbounds = []
    tags_seen = set()
    for u in uris:
        p = parse_uri(u)
        if not p:
            continue
        ob = uri_to_singbox_outbound(p)
        if ob is None:
            continue
        tag = ob['tag']
        if tag in tags_seen:
            continue
        tags_seen.add(tag)
        outbounds.append(ob)
    config = {
        'log': {'level': 'info', 'timestamp': True},
        'dns': {'servers': [{'tag': 'google', 'address': '8.8.8.8'}]},
        'inbounds': [{'type': 'mixed', 'tag': 'mixed-in', 'listen': '::', 'listen_port': 2080}],
        'outbounds': outbounds,
        'route': {'final': 'proxy', 'auto_detect_interface': True, 'rule_set': [], 'rules': []},
    }
    if outbounds:
        tags = [o['tag'] for o in outbounds]
        config['outbounds'].insert(0, {'type': 'selector', 'tag': 'proxy',
                                       'default': tags[0] if tags else 'direct',
                                       'outbounds': ['direct'] + tags})
        config['outbounds'].append({'type': 'direct', 'tag': 'direct'})
        config['outbounds'].append({'type': 'block', 'tag': 'block'})
        config['route']['rules'] = [{'ip_is_private': True, 'outbound': 'direct'}, {'outbound': 'proxy'}]
    return json.dumps(config, indent=2, ensure_ascii=False), len(outbounds)


# ──── 组合生成 ────
def combo_uris(cfg, combo):
    """收集组合引用的所有源缓存 URI (跳过 停用/缺失缓存 的源)"""
    uris = []
    for sid in combo.get('sources', []):
        src = find_source(cfg, sid)
        if src is None or not src.get('enabled', True):
            continue
        uris.extend(read_cache(sid))
    return uris


def generate_combo(cfg, combo):
    """
    从缓存生成某组合的 3 个文件。
    返回 (node_count, content_hash) 或 (0, '') (无节点)。content_hash 用于比对是否变化。
    """
    slug = combo['slug']
    uris = combo_uris(cfg, combo)
    # base64
    b64raw = '\n'.join(uris)
    b64txt = base64.b64encode(b64raw.encode('utf-8')).decode('ascii')
    # clash
    clash_yaml, c_n = generate_clash(uris)
    # singbox
    sb_json, s_n = generate_singbox(uris)
    # node_count = 真实对外节点数 (clash 去重 proxies, 不含 selector/group/direct/block)
    n = c_n
    new_hash = sha(b64raw)
    with open(os.path.join(OUT_DIR, f'{slug}_sub.txt'), 'w', encoding='utf-8') as f:
        f.write(b64txt)
    with open(os.path.join(OUT_DIR, f'{slug}_clash.yaml'), 'w', encoding='utf-8') as f:
        f.write(clash_yaml)
    with open(os.path.join(OUT_DIR, f'{slug}_singbox.json'), 'w', encoding='utf-8') as f:
        f.write(sb_json)
    return n, new_hash


def regenerate_combo(cfg, combo, save=True):
    """重生成组合文件, 若内容变化更新 updated_at + content_hash。返回 changed:bool"""
    n, new_hash = generate_combo(cfg, combo)
    changed = (new_hash != combo.get('content_hash', ''))
    combo['node_count'] = n
    if changed:
        combo['content_hash'] = new_hash
        combo['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    elif not combo.get('updated_at'):
        combo['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    if save:
        save_config(cfg)
    return changed


# ──── 高级操作 (供 server.py 调用) ────
def update_all():
    """
    全部更新: 抓所有 enabled 源(独立, 失败隔离) -> 重生成内容有变的组合。
    返回汇总 dict 供反馈面板。
    """
    cfg = load_config()
    t0 = time.time()
    fetched = []
    changed_ids = []
    for src in cfg.get('sources', []):
        if not src.get('enabled', True):
            continue
        res = refresh_source(src, 30)
        fetched.append({'id': res['id'], 'ok': res['ok'], 'changed': res['changed'],
                        'error': res['error'], 'node_count': res['node_count']})
        if res['ok'] and res['changed']:
            changed_ids.append(src['id'])
    save_config(cfg)
    # 重生成: 引用了 changed 源, 或从未生成过(updated_at 空)的组合
    regenerated = []
    for combo in cfg.get('combos', []):
        refs = combo.get('sources', [])
        if not combo.get('updated_at') or any(s in changed_ids for s in refs):
            was = regenerate_combo(cfg, combo, save=False)
            regenerated.append({'slug': combo['slug'], 'changed': was,
                                'node_count': combo['node_count']})
    save_config(cfg)
    return {
        'fetched': fetched,
        'changed_sources': changed_ids,
        'regenerated': regenerated,
        'duration': round(time.time() - t0, 1),
    }


def update_source(src_id):
    """单源更新: 重抓该源 + 重生成引用它的组合。返回汇总。"""
    cfg = load_config()
    src = find_source(cfg, src_id)
    if src is None:
        return {'error': '源不存在'}
    t0 = time.time()
    res = refresh_source(src, 30)
    save_config(cfg)
    regenerated = []
    if res['ok'] and res['changed']:
        for combo in cfg.get('combos', []):
            if src_id in combo.get('sources', []):
                was = regenerate_combo(cfg, combo, save=False)
                regenerated.append({'slug': combo['slug'], 'changed': was,
                                    'node_count': combo['node_count']})
        save_config(cfg)
    return {
        'fetched': [{'id': res['id'], 'ok': res['ok'], 'changed': res['changed'],
                     'error': res['error'], 'node_count': res['node_count']}],
        'regenerated': regenerated,
        'duration': round(time.time() - t0, 1),
    }


def delete_source(src_id):
    """删源: 从源池移除 + 从所有组合摘除 + 删缓存 + 重生成受影响组合。返回汇总。"""
    cfg = load_config()
    src = find_source(cfg, src_id)
    if src is None:
        return {'error': '源不存在'}
    cfg['sources'] = [s for s in cfg['sources'] if s['id'] != src_id]
    cp = cache_path(src_id)
    if os.path.exists(cp):
        os.remove(cp)
    regenerated = []
    for combo in cfg['combos']:
        if src_id in combo.get('sources', []):
            combo['sources'] = [s for s in combo['sources'] if s != src_id]
            was = regenerate_combo(cfg, combo, save=False)
            regenerated.append({'slug': combo['slug'], 'changed': was,
                                'node_count': combo['node_count']})
    save_config(cfg)
    return {'deleted': src_id, 'regenerated': regenerated}


def delete_combo(slug):
    """删组合: 从配置移除 + 删其 3 个文件。"""
    cfg = load_config()
    cfg['combos'] = [c for c in cfg['combos'] if c['slug'] != slug]
    save_config(cfg)
    for ext in ('_sub.txt', '_clash.yaml', '_singbox.json'):
        p = os.path.join(OUT_DIR, slug + ext)
        if os.path.exists(p):
            os.remove(p)
    return {'deleted': slug}


def ensure_source_fetch_on_add(src_id):
    """新增/恢复源后抓一次, 让缓存可用。返回 fetch 结果。"""
    cfg = load_config()
    src = find_source(cfg, src_id)
    if src is None:
        return None
    res = refresh_source(src, 30)
    save_config(cfg)
    # 重生成引用它的组合
    regenerated = []
    if res['ok']:
        for combo in cfg.get('combos', []):
            if src_id in combo.get('sources', []):
                was = regenerate_combo(cfg, combo, save=False)
                regenerated.append({'slug': combo['slug'], 'changed': was})
        save_config(cfg)
    return {'fetch': res, 'regenerated': regenerated}


# ──── 命令行入口 (手动 / merge_sub.sh 调用) ────
def main():
    cfg = load_config()
    if not cfg.get('sources'):
        print('ERROR: 无订阅源')
        sys.exit(1)
    summary = update_all()
    print(f"[=] 抓取 {len(summary['fetched'])} 个源, 内容变化 {len(summary['changed_sources'])} 个")
    for f in summary['fetched']:
        st = 'OK' if f['ok'] else 'FAIL'
        print(f"    [{st}] {f['id']}: {f['node_count']} 节点" + (f" ({f['error']})" if f['error'] else ''))
    print(f"[=] 重生成组合: {', '.join(r['slug'] for r in summary['regenerated']) or '无'}")
    print(f"[=] 耗时 {summary['duration']}s")


def write_nginx_config():
    """
    依据当前 combos 重新生成 Nginx 配置 (不加源抓取), cp 到 vhost + nginx -t + reload。
    组合新增/删除会改变端点路径集, 必须调用本函数。
    """
    import subprocess
    cfg = load_config()
    combos = cfg.get('combos', [])
    SUB = SUB_NAME
    L = []
    L.append('server {')
    L.append('    listen 443 ssl http2;')
    L.append('    server_name %s;' % DOMAIN)
    L.append('    ssl_certificate     %s;' % NGINX_CERT_FULL)
    L.append('    ssl_certificate_key %s;' % NGINX_CERT_KEY)
    L.append('    ssl_protocols TLSv1.2 TLSv1.3;')
    L.append('    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;')
    L.append('')
    for c in combos:
        slug = c['slug']
        L.append('    # combo %s' % slug)
        L.append('    location = /%s {' % slug)
        L.append('        add_header Content-Type "text/plain; charset=utf-8";')
        L.append('        add_header Content-Disposition "attachment; filename=\\"%s.txt\\"";' % SUB)
        L.append('        alias /opt/sub-converter/%s_sub.txt;' % slug)
        L.append('    }')
        L.append('    location = /%s/clash {' % slug)
        L.append('        default_type text/yaml;')
        L.append('        add_header Content-Disposition "attachment; filename=\\"%s.yaml\\"";' % SUB)
        L.append('        alias /opt/sub-converter/%s_clash.yaml;' % slug)
        L.append('    }')
        L.append('    location = /%s/singbox {' % slug)
        L.append('        default_type application/json;')
        L.append('        add_header Content-Disposition "attachment; filename=\\"%s.json\\"";' % SUB)
        L.append('        alias /opt/sub-converter/%s_singbox.json;' % slug)
        L.append('    }')
        L.append('')
    L.append('    location = /admin {')
    L.append('        return 301 /admin/;')
    L.append('    }')
    L.append('    location /admin/ {')
    L.append('        proxy_pass http://127.0.0.1:8088;')
    L.append('        proxy_set_header Host $host;')
    L.append('        proxy_set_header X-Real-IP $remote_addr;')
    L.append('        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;')
    L.append('        proxy_set_header X-Forwarded-Proto $scheme;')
    L.append('    }')
    L.append('}')
    tmp = '/opt/sub-converter/.nginx_conf.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(L) + '\n')
    nginx_conf = NGINX_CONF_PATH
    subprocess.run(['cp', tmp, nginx_conf], check=True)
    r = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('nginx -t failed: ' + r.stderr)
    subprocess.run(['nginx', '-s', 'reload'], check=True)
    return True


if __name__ == '__main__':
    main()
