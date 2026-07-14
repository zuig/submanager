#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZSUB 订阅管理面板 v2 —— 源池 + 多组合
监听 127.0.0.1:8088, Nginx 反代 /admin/
依赖: 同目录上级的 convert.py (from convert import ...)
"""
import sys, os, json, hashlib, time, urllib.parse, subprocess, socket, threading, urllib.request
sys.path.insert(0, '/opt/sub-converter')
import convert

# 管理密码：优先读环境变量 ZSM_PASS；未设置时请用 install.sh 生成并写入 .env
# 注意：仓库默认值为占位符，部署时务必改为强密码（否则任何人可登录面板）
PASSWORD = os.environ.get('ZSM_PASS', 'CHANGE_ME_ZSM_ADMIN_PASSWORD')
# 订阅域名：用于面板展示各组合端点 URL，部署时通过 SUB_DOMAIN 环境变量覆盖
DOMAIN = os.environ.get('SUB_DOMAIN', 'sub.example.com')
SECRET = hashlib.sha256(PASSWORD.encode()).hexdigest()[:32]
SESSIONS = {}  # token -> True (内存, 重启清空, 可接受)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def check_session(handler):
    ck = handler.headers.get('Cookie', '')
    for part in ck.split(';'):
        part = part.strip()
        if part.startswith('zsm_session='):
            token = part.split('=', 1)[1]
            return token in SESSIONS
    return False


def gen_token():
    return hashlib.sha256((SECRET + str(time.time()) + os.urandom(8).hex()).encode()).hexdigest()


# ──── 派生展示数据 ────
def state_payload():
    cfg = convert.load_config()
    for s in cfg['sources']:
        s['_title'] = s['name']
        # 计算引用该源的组合数
        s['_ref_count'] = sum(1 for c in cfg.get('combos', []) if s['id'] in c.get('sources', []))
        # 节点数为0但缓存存在时,从缓存文件补(未抓取或初始状态)
        if not s.get('node_count'):
            try:
                s['node_count'] = len(convert.read_cache(s['id']))
            except Exception:
                pass
    # 构建源ID→名称的映射，供组合卡片展示
    src_name_map = {s['id']: s['name'] for s in cfg['sources']}
    for c in cfg['combos']:
        c['_title'] = c['remark'] if c.get('remark') else f"{c['slug']}组合"
        src_names = [src_name_map.get(sid, sid) for sid in (c['sources'] or [])]
        c['_sub'] = c['slug'] + ' · ' + (' · '.join(src_names) if src_names else '空')
        c['_b64'] = f"https://{DOMAIN}/{c['slug']}"
        c['_clash'] = f"https://{DOMAIN}/{c['slug']}/clash"
        c['_singbox'] = f"https://{DOMAIN}/{c['slug']}/singbox"
    normal = sum(1 for s in cfg['sources'] if s.get('enabled', True) and s.get('status') == 'ok')
    abnormal = sum(1 for s in cfg['sources'] if (not s.get('enabled', True)) or s.get('status') == 'fail')
    return {
        'sources': cfg['sources'],
        'combos': cfg['combos'],
        'stats': {
            'sources': len(cfg['sources']),
            'combos': len(cfg['combos']),
            'normal': normal,
            'abnormal': abnormal,
        }
    }


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZSUB 订阅管理</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e6e8eb;--blue:#2f6bff;--green:#1f9d55;--red:#e0483e;--amber:#e08a00;--mut:#8a9099;--txt:#1f2329}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);font-size:14px}
.topbar{display:flex;align-items:center;gap:12px;padding:14px 22px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.topbar h1{font-size:17px;margin:0;font-weight:700}
.topbar .sp{flex:1}
.btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px;color:var(--txt)}
.btn:hover{background:#f0f2f5}
.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn.primary:hover{background:#245ae0}
.btn.danger{color:var(--red);border-color:#f3c6c2}
.btn.sm{padding:4px 9px;font-size:12px}
.btn:disabled{opacity:.55;cursor:not-allowed}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 80px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .n{font-size:26px;font-weight:700}
.card .l{color:var(--mut);font-size:12px;margin-top:2px}
.feedback{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:10px;padding:12px 16px;margin-bottom:18px;display:none}
.feedback.show{display:block}
.feedback h3{margin:0 0 8px;font-size:14px}
.feedback .row{font-size:13px;line-height:1.7;color:#3a3f47}
.feedback .ok{color:var(--green)}.feedback .fail{color:var(--red)}.feedback .mut{color:var(--mut)}
.sec-title{font-size:15px;font-weight:700;margin:22px 0 10px;display:flex;align-items:center;gap:10px}
.sec-title .add{margin-left:auto}
.pool{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:middle}
th{background:#fafbfc;color:var(--mut);font-weight:600;font-size:12px;white-space:nowrap}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600}
.tag.ok{background:#e6f6ec;color:var(--green)}
.tag.fail{background:#fdecea;color:var(--red)}
.tag.off{background:#eef0f2;color:var(--mut)}
.tag.chg{background:#fff3e0;color:var(--amber)}
.ops{display:flex;gap:6px;flex-wrap:wrap}
.combos{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.combo{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column}
.combo h4{margin:0;font-size:16px}
.combo .sub{color:var(--mut);font-size:12px;margin:3px 0 10px}
.combo .meta{display:flex;gap:14px;font-size:12px;color:#3a3f47;margin-bottom:10px}
.combo .meta b{color:var(--txt)}
.ep{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.ep .u{flex:1;background:#f7f8fa;border:1px solid var(--line);border-radius:7px;padding:6px 9px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.combo .foot{margin-top:auto;display:flex;gap:8px;padding-top:10px;border-top:1px solid var(--line)}
.note{color:var(--mut);font-size:12px;margin-top:10px;line-height:1.6}
.mask{position:fixed;inset:0;background:rgba(20,24,30,.45);display:none;align-items:center;justify-content:center;z-index:50}
.mask.show{display:flex}
.modal{background:#fff;border-radius:14px;padding:20px 22px;width:440px;max-width:92vw;max-height:88vh;overflow:auto}
.modal h3{margin:0 0 14px;font-size:16px}
.field{margin-bottom:13px}
.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:5px}
.field input,.field textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px;font-family:inherit}
.field .err{color:var(--red);font-size:12px;margin-top:5px;display:none}
.srcpick{display:block !important;max-height:200px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;padding:8px 10px;box-sizing:border-box;width:100%}
.srcpick *{box-sizing:border-box}
.srcpick label{display:block !important;float:none !important;padding:5px 4px;font-size:13px;cursor:pointer;line-height:1.6;border-radius:4px}
.srcpick label input[type="checkbox"]{display:inline-block !important;margin:0 8px 0 0 !important;vertical-align:middle;cursor:pointer;float:none !important}
.srcpick label:hover{background:#f7f8fa}
.modal .acts{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
.warn{background:#fdecea;border:1px solid #f3c6c2;color:#a8342c;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:12px;line-height:1.6}
.empty{color:var(--red);font-size:12px;margin-top:6px;display:none}
/* 日志面板 */
.log-panel{max-height:60vh;overflow-y:auto;padding-right:4px}
.log-group{background:#f8f9fb;border:1px solid #e9ecef;border-radius:10px;margin-bottom:12px;overflow:hidden}
.log-group-head{background:#fff;display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #f0f0f0;font-size:13px}
.log-group-head .time{color:#636e7b;font-weight:500}
.log-group-head .badge{font-size:11px;background:#eef2ff;color:#4f6ef7;border-radius:10px;padding:2px 10px}
.log-group-body{padding:10px 14px;font-size:12.5px;line-height:1.85}
.log-line{display:flex;align-items:center;padding:1px 0;gap:6px}
.log-tag{flex-shrink:0;width:44px;text-align:center;font-size:11px;font-weight:600;border-radius:4px;padding:1px 0}
.log-tag.ok{background:#ecfdf5;color:#059669}
.log-tag.info{background:#f1f5f9;color:#64748b}
.log-tag.sys{background:#fefce8;color:#a16207}
.log-tag.err{background:#fef2f2;color:#dc2626}
.log-text{color:#374151}
.log-text .dim{color:#9ca3af}
.log-text .hl{color:#2563eb;font-weight:500}

/* 预览弹窗 */
.preview-modal{width:680px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column}
.preview-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.preview-head h3{margin:0;font-size:15px}
.preview-head .fmt{font-size:12px;color:#64748b;background:#f1f5f9;padding:3px 10px;border-radius:5px;font-weight:500}
.preview-body{flex:1;overflow:auto;border:1px solid #e8ecf0;border-radius:8px;background:#fafbfc}
.preview-body pre{margin:0;padding:14px 16px;font-size:11.8px;line-height:1.7;font-family:ui-monospace,Menlo,Consolas,monospace;color:#374151;white-space:pre-wrap;word-break:break-all}
.preview-body .node-item{padding:4px 0;border-bottom:1px dashed #eef0f2}
.preview-body .node-item:last-child{border-bottom:none}
.preview-body .node-name{color:#2563eb;font-weight:600}
.preview-body .node-uri{color:#6b7280;font-size:11.5px;word-break:break-all}
.preview-foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
.preview-foot .cnt{font-size:12px;color:#64748b}

.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);background:#1f2329;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.25s;z-index:80;pointer-events:none}
.toast.show{opacity:1}

/* 拖拽排序 */
.grip{cursor:grab;user-select:none;padding:0 4px;line-height:1;display:flex;align-items:center}
.grip svg{opacity:.35;transition:opacity .15s}
.grip:hover svg{opacity:.7}
.grip:active{cursor:grabbing}
tr[draggable]{transition:background .15s}
tr.dragging{opacity:.45;background:#eef3fc}
tr.drag-over{border-top:2px solid var(--blue)}
.ipq-loading{color:var(--mut);padding:14px 0}
.ipq-hero{display:flex;gap:12px;margin:6px 0 10px}
.ipq-badge{flex:1;border-radius:10px;padding:12px 14px;background:#f4f6f8;border:1px solid var(--line);text-align:center}
.ipq-badge .lbl{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
.ipq-badge b{font-size:18px}
.ipq-badge.ok{background:#e8f6ee;border-color:#bfe3cd}
.ipq-badge.warn{background:#fff6e6;border-color:#f3dca6}
.ipq-badge.bad{background:#fdecea;border-color:#f3c6c2}
.tag-dc b{color:#2b6cb0}.tag-res b{color:#2f855a}.tag-mob b{color:#805ad5}.tag-proxy b{color:#c05621}
.ipq-meta{font-size:12px;color:var(--txt);margin-bottom:10px}
.ipq-meta .mut{color:var(--mut)}
.ipq-detail-toggle{margin-bottom:8px}
.ipq-detail{background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px}
table.kv{width:100%;border-collapse:collapse;font-size:13px}
table.kv td{padding:5px 8px;border-bottom:1px solid var(--line)}
table.kv td:first-child{color:var(--mut);width:90px;white-space:nowrap}
.ipq-bl-title{font-size:13px;font-weight:600;margin:10px 0 4px}
ul.ipq-bl{margin:0;padding-left:18px;font-size:13px}
ul.ipq-bl li{margin:2px 0}
</style>
</head>
<body>
<div class="topbar">
  <h1>ZSUB 订阅管理</h1>
  <span class="sp"></span>
  <button class="btn primary" id="btnUpdateAll">全部更新</button>
  <button class="btn" id="btnLog">查看日志</button>
  <button class="btn danger" id="btnLogout">退出</button>
</div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="feedback" id="feedback"></div>

  <div class="sec-title">订阅源池 <button class="btn sm primary add" id="btnAddSrc" onclick="btnAddSrc()">+ 新增源</button></div>
  <div class="pool">
    <table>
      <thead><tr><th></th><th>源</th><th>IP</th><th>节点数</th><th>引用</th><th>状态</th><th>最后抓取</th><th>节点变动</th><th>操作</th></tr></thead>
      <tbody id="srcBody"></tbody>
    </table>
  </div>
  <div class="note">拖拽左侧 ⠿ 图标可调整源顺序。更新某源或点「全部更新」后，上方面板会显示本次抓了哪些源、重生成了哪些组合、谁失败降级。节点变动时间停在源节点内容上一次实际变化的时刻。</div>

  <div class="sec-title">订阅组合 <button class="btn sm primary add" id="btnAddCombo" onclick="btnAddCombo()">+ 新建组合</button></div>
  <div class="combos" id="combos"></div>
</div>

<!-- 通用弹窗 -->
<div class="mask" id="mask"><div class="modal" id="modal"></div></div>
<div class="toast" id="toast"></div>

<script>
const $ = (s)=>document.querySelector(s);
const $$ = (s)=>document.querySelectorAll(s);
let STATE = {sources:[],combos:[]};
let draggedRow = null;

function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800);}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmtTime(t){if(!t)return '\u2014';return t.replace('T',' ');}

async function api(path,body){
  const opt={method:body?'POST':'GET',headers:{}};
  if(body){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body);}
  const r=await fetch('/admin/api/'+path,opt);
  if(r.status===401){location.reload();throw new Error('unauth');}
  return r.json();
}

function statusTag(s){
  if(!s.enabled) return '<span class="tag off">\u5df2\u505c\u7528</span>';
  if(s.status==='fail') return '<span class="tag fail">\u5931\u6548</span>';
  if(s.status==='ok') return '<span class="tag ok">\u6b63\u5e38</span>';
  return '<span class="tag off">\u672a\u6293\u53d6</span>';
}

function refBadge(n){
  if(!n) return '<span style="color:var(--mut)">0</span>';
  var c=(n>=2)?'var(--blue)':'var(--txt)';
  return '<b style="color:'+c+'">'+n+'</b>';
}

function render(){
  const st=STATE.stats;
  $('#cards').innerHTML=
    card(st.sources,'\u8ba2\u9605\u6e90')+card(st.combos,'\u8ba2\u9605\u7ec4\u5408')+card(st.normal,'\u6b63\u5e38\u6e90')+card(st.abnormal,'\u5f02\u5e38\u6e90');
  // \u6e90\u6c60
  $('#srcBody').innerHTML = STATE.sources.map((s,idx)=>{
    const rid='sr_'+esc(s.id);
    return '<tr draggable="true" data-id="'+esc(s.id)+'" data-idx="'+idx+'" id="'+rid+'">'+
      '<td><span class="grip" title="\u62d6\u62fd\u6392\u5e8f"><svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor"><circle cx="3" cy="2.5" r="1.5"/><circle cx="10.5" cy="2.5" r="1.5"/><circle cx="3" cy="7" r="1.5"/><circle cx="10.5" cy="7" r="1.5"/><circle cx="3" cy="11.5" r="1.5"/><circle cx="10.5" cy="11.5" r="1.5"/></svg></span></td>'+
      '<td><b>'+esc(s.name)+'</b><div class="mono mut" style="color:var(--mut);font-size:11px">'+(esc(s.location||'')||'\u2014')+'</div></td>'+
      '<td class="mono">'+esc(s.ip||'')+'</td>'+
      '<td><b>'+(s.node_count||0)+'</b></td>'+
      '<td>'+refBadge(s._ref_count)+'</td>'+
      '<td>'+statusTag(s)+'</td>'+
      '<td class="mono">'+fmtTime(s.last_fetch)+'</td>'+
      '<td class="mono">'+(fmtTime(s.last_change)||'\u2014')+'</td>'+
      '<td><div class="ops">'+
        '<button class="btn sm" onclick="srcFetch(\''+s.id+'\')">更新</button>'+
        '<button class="btn sm" onclick="srcEdit(\''+s.id+'\')">编辑</button>'+
        '<button class="btn sm" onclick="srcToggle(\''+s.id+'\')">'+(s.enabled?'\u505c\u7528':'\u542f\u7528')+'</button>'+
        '<button class="btn sm" onclick="srcIpQ(\''+s.id+'\')">IP质量</button>'+
        '<button class="btn sm danger" onclick="srcDel(\''+s.id+'\')">删除</button>'+
      '</div></td>'+
    '</tr>';
  }).join('');
  bindDragDrop();
  // 组合
  $('#combos').innerHTML = STATE.combos.map(c=>`
    <div class="combo">
      <h4>${esc(c._title)}</h4>
      <div class="sub">${esc(c._sub)}</div>
      <div class="meta"><span>节点 <b>${c.node_count}</b></span><span>更新 <b>${fmtTime(c.updated_at)}</b></span></div>
      <div class="ep"><div class="u" title="${esc(c._b64)}">${esc(c._b64)}</div><button class="btn sm" onclick="previewEp('${esc(c._b64)}','Base64')">预览</button><button class="btn sm" onclick="copy('${esc(c._b64)}')">复制</button></div>
      <div class="ep"><div class="u" title="${esc(c._clash)}">${esc(c._clash)}</div><button class="btn sm" onclick="previewEp('${esc(c._clash)}','Clash')">预览</button><button class="btn sm" onclick="copy('${esc(c._clash)}')">复制</button></div>
      <div class="ep"><div class="u" title="${esc(c._singbox)}">${esc(c._singbox)}</div><button class="btn sm" onclick="previewEp('${esc(c._singbox)}','Sing-box')">预览</button><button class="btn sm" onclick="copy('${esc(c._singbox)}')">复制</button></div>
      <div class="foot">
        <button class="btn sm" onclick="comboEdit('${c.slug}')">编辑</button>
        <button class="btn sm danger" onclick="comboDel('${c.slug}')">删除</button>
      </div>
    </div>`).join('');
}
function card(n,l){return '<div class="card"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>';}

async function refresh(){STATE=await api('state');render();}
async function copy(t){try{await navigator.clipboard.writeText(t);toast('\u5df2 复制');}catch(e){toast('\u590d\u5236\u5931\u8d25,\u8bf7\u624b\u52a8\u9009\u62e9');}}

// 预览订阅内容
async function previewEp(url,label){
  const mask=$('#mask');const modal=$('#modal');
  modal.className='modal preview-modal';
  openModal('<div class="preview-head"><h3>'+esc(label)+' 订阅预览</h3><span class="fmt">加载中…</span></div><div class="preview-body"><pre>轻等…</pre></div><div class="preview-foot"><span class="cnt"></span><button class="btn sm" onclick="closeModal()">关闭</button></div>');
  try{
    const r=await api('preview',{url});
    if(r.error){modal.querySelector('pre').textContent='\u274c '+r.error;return;}
    const text=r.content||'';
    let html='',nodeCount=0;
    if(label==='Base64'||text.match(new RegExp('^(hysteria2|vless|vmess|trojan|ss|tuic|anytls)://','m'))){
      // 按 \\n / \\r\\n / \\r 分割（JS 中必须是真正的换行转义）
      let lines=text.trim().split(/\r?\n/);
      // 兜底：如果以上都只得到1行但含多个协议前缀，用 match 提取所有 URI
      const protoRe=/\b(hysteria2|vless|vmess|trojan|ss|tuic|anytls):\/\//g;
      if(lines.length<=1 && text.match(protoRe)?.length>=2){
        protoRe.lastIndex=0;
        let m;lines=[];
        while((m=protoRe.exec(text))!==null){lines.push(m[0]+text.slice(protoRe.lastIndex).split(/[\r\n]/)[0]);}
        // 补齐 lastIndex 偏移
        let pos=0,out=[];protoRe.lastIndex=0;
        while((m=protoRe.exec(text))!==null){out.push(text.slice(m.index));pos=m.index;}
        lines=out;
      }
      lines=lines.filter(l=>l.trim());
      nodeCount=lines.length;
      html='<pre>';
      for(const line of lines){
        let name=line.split('#').pop()||line.slice(0,80)+'…';
        name=decodeURIComponent(name);
        html+='<div class="node-item"><span class="node-name">'+esc(name)+'</span><br><span class="node-uri">'+esc(line)+'</span></div>';
      }
      html+='</pre>';
    }else if(label==='Clash'||text.includes('proxies:')){
      // 只统计 proxies 段内的 - name:（排除 proxy-groups 段的组名）
      const lines=text.split('\n');
      let inProxies=false;
      nodeCount=0;
      for(const l of lines){
        const t=l.trim();
        if(t==='proxies:'){inProxies=true;continue;}
        if(t.match(/^(proxy-groups|rules|dns):/)) break; // 离开 proxies 段
        if(inProxies && t.startsWith('- name:')) nodeCount++;
      }
      html='<pre>'+esc(text)+'</pre>';
    }else if(label==='Sing-box'){
      try{const j=JSON.parse(text);nodeCount=(j.outbounds||[]).filter(o=>['hysteria2','vless','tuic','anytls','trojan','vmess','shadowsocks'].includes(o?.type)).length;}catch(e){}
      html='<pre style="white-space:pre-wrap">'+esc(text)+'</pre>';
    }
    modal.querySelector('.preview-body').innerHTML=html;
    modal.querySelector('.fmt').textContent=label+(label==='Base64'?' (已解码)':'')+'  ·  '+text.length+' 字符';
    modal.querySelector('.cnt').textContent='共 '+nodeCount+' 节点';
  }catch(e){
    modal.querySelector('pre').textContent='❌ 预览失败: '+(e.message||e);
  }
}

// 反馈面板
function showFeedback(sum,title){
  const f=$('#feedback');
  let rows='';
  if(sum.fetched){
    rows+='<div class="row"><b>\u6293\u53d6\u6e90\uff1a</b>';
    rows+=sum.fetched.map(x=>'<span class="'+(x.ok?'ok':'fail')+'">'+esc(x.id)+' '+(x.ok?(x.node_count+'\u8282\u70b9'):(('\u5931\u8d25')+(x.error?('('+esc(x.error)+')'):'')))+'</span>').join(' · ');
    rows+='</div>';
  }
  if(sum.changed_sources&&sum.changed_sources.length)
    rows+='<div class="row mut">\u8282\u70b9\u53d8\u5316\u7684\u6e90\uff1a'+sum.changed_sources.map(esc).join('\u3001')+'</div>';
  if(sum.regenerated){
    rows+='<div class="row"><b>\u91cd\u751f\u6210\u7ec4\u5408\uff1a</b>'+sum.regenerated.map(r=>'<span class="ok">'+esc(r.slug)+'</span>'+(r.changed?'':'<span class="mut">(无变化)</span>')).join(' · ')+'</div>';
  }
  if(sum.duration!=null) rows+='<div class="row mut">\u8017\u65f6 '+sum.duration+'s</div>';
  f.innerHTML='<h3>'+esc(title||'更新结果')+'</h3>'+rows;
  f.classList.add('show');
}

// 全部更新
$('#btnUpdateAll').onclick=async()=>{
  const b=$('#btnUpdateAll');b.disabled=true;b.textContent='\u66f4\u65b0\u4e2d\u2026';
  try{const sum=await api('update-all',{});showFeedback(sum,'\u5168\u90e8\u66f4\u65b0');await refresh();}
  catch(e){}finally{b.disabled=false;b.textContent='\u5168\u90e8\u66f4\u65b0';}
};
$('#btnLogout').onclick=async()=>{await api('logout',{});location.reload();};
$('#btnLog').onclick=async()=>{const log=await api('log');openLog(log.log||'\\u6682\\u65e0\\u65e5\\u5fd7');};

// 源操作
async function srcFetch(id){const sum=await api('source/fetch',{id});showFeedback(sum,'更新源 '+id);await refresh();}
async function srcToggle(id){const s=STATE.sources.find(x=>x.id===id);await api('source/toggle',{id,enabled:!s.enabled});await refresh();toast(s.enabled?'\u5df2\u505c\u7528':'\u5df2\u542f\u7528');}
async function srcIpQ(id){
  const s=STATE.sources.find(x=>x.id===id);
  if(!s){return;}
  const ip=(s.ip||'').trim();
  if(!ip){openModal('<h3>IP 质量</h3><div class="warn">该源没有 IP，请先在「编辑」里补充 IP 后再检测。</div><div class="acts"><button class="btn primary" onclick="closeModal()">知道了</button></div>');return;}
  openModal('<h3>IP 质量 · '+esc(s.name)+'</h3><div id="ipqBody" class="ipq-loading">检测中…</div>');
  try{
    const r=await api('ip-quality',{ip,force:false});
    renderIpq(r, ip);
  }catch(e){ const b=document.getElementById('ipqBody'); if(b)b.innerHTML='<div class="warn">检测失败：'+esc(e.message)+'</div>'; }
}
function renderIpq(r, ip){
  const b=document.getElementById('ipqBody'); if(!b)return;
  if(!r.ok){ b.innerHTML='<div class="warn">'+esc(r.error||'检测失败')+'</div>'; return; }
  const type=r.ip_type||'—', rep=r.reputation||'—';
  const repClass = rep==='干净'?'ok':(rep==='可疑'?'warn':(rep==='滥用'?'bad':''));
  const typeClass = type.indexOf('机房')>=0?'tag-dc':(type.indexOf('住宅')>=0?'tag-res':(type.indexOf('移动')>=0?'tag-mob':'tag-proxy'));
  const total=(r.dnsbl&&r.dnsbl.total)||0;
  let html='';
  html+='<div class="ipq-hero">';
  html+='<div class="ipq-badge '+typeClass+'"><span class="lbl">IP 类型</span><b>'+esc(type)+'</b></div>';
  html+='<div class="ipq-badge '+repClass+'"><span class="lbl">声誉评级</span><b>'+esc(rep)+'</b></div>';
  html+='</div>';
  html+='<div class="ipq-meta">IP <b>'+esc(ip)+'</b> · 黑名单命中 <b>'+(r.blacklist_hits||0)+'/'+total+'</b>'+(r.cached?' · <span class="mut">缓存('+(r.count||0)+'次)</span>':' · <span class="mut">刚检测</span>')+'</div>';
  html+='<div class="ipq-detail-toggle"><button class="btn sm" onclick="toggleIpqDetail()">详细 ▾</button></div>';
  html+='<div id="ipqDetail" class="ipq-detail" style="display:none">';
  const g=r.geo||{};
  if(g.status==='success'||g.country){
    const rows=[['国家',g.country+(g.countryCode?' ('+g.countryCode+')':'')],['地区',(g.regionName||'')+(g.city?' / '+g.city:'')],['ISP',g.isp],['组织',g.org],['ASN',(g.as||'')+(g.asname?' '+g.asname:'')],['时区',g.timezone],['经纬度',(g.lat!=null&&g.lon!=null)?(g.lat+', '+g.lon):''],['反向DNS',g.reverse],['标记',[g.proxy?'proxy':'',g.hosting?'hosting':'',g.mobile?'mobile':''].filter(Boolean).join(' ')||'—']];
    html+='<table class="kv">';
    for(const kv of rows){ html+='<tr><td>'+esc(kv[0])+'</td><td>'+esc(kv[1]||'—')+'</td></tr>'; }
    html+='</table>';
  }else{
    html+='<div class="warn">GeoIP 查询失败：'+esc(g.message||'未知')+'</div>';
  }
  if(r.dnsbl&&r.dnsbl.lists&&r.dnsbl.lists.length){
    html+='<div class="ipq-bl-title">黑名单命中清单</div><ul class="ipq-bl">';
    for(const it of r.dnsbl.lists){ html+='<li>'+esc(it.name)+' <span class="mut">'+esc(it.code||'')+'</span></li>'; }
    html+='</ul>';
  }
  html+='</div>';
  html+='<div class="acts"><button class="btn primary" onclick="refreshIpq(\''+esc(ip)+'\')">刷新数据</button><button class="btn" onclick="closeModal()">关闭</button></div>';
  b.innerHTML=html;
}
function toggleIpqDetail(){ const d=document.getElementById('ipqDetail'); if(d)d.style.display = d.style.display==='none'?'block':'none'; }
async function refreshIpq(ip){
  const b=document.getElementById('ipqBody'); if(b)b.innerHTML='检测中…';
  try{ const r=await api('ip-quality',{ip,force:true}); renderIpq(r, ip); }
  catch(e){ if(b)b.innerHTML='<div class="warn">刷新失败：'+esc(e.message)+'</div>'; }
}

function srcEdit(id){
  const s=STATE.sources.find(x=>x.id===id);
  openModal(`
    <h3>\u7f16\u8f91\u6e90</h3>
    <div class="field"><label>\u540d\u79f0</label><input id="m_name" value="${esc(s.name)}"></div>
    <div class="field"><label>\u6240\u5728\u5730 <button class="btn sm" onclick="srcResolveLocation('${s.id}')" style="margin-left:8px" id="btnRLoc">\u4eceIP\u83b7\u53d6</button></label><input id="m_location" value="${esc(s.location||'')}" placeholder="\u5982 \u9999\u6e2f\u3001\u6d1b\u6715\u77ed"></div>
    <div class="field"><label>IP <button class="btn sm" onclick="srcResolveIp('${s.id}')" style="margin-left:8px" id="btnRIp">\u4ece\u57df\u540d\u89e3\u6790</button></label><input id="m_ip" value="${esc(s.ip||'')}"></div>
    <div class="field"><label>\u8ba2\u9605 URL</label><input id="m_url" value="${esc(s.url)}"></div>
    <div class="field err" id="m_err"></div>
    <div class="acts"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="srcSave('${s.id}')">\u4fdd\u5b58</button></div>
  `);
}
async function srcSave(id){
  const name=$('#m_name').value.trim(),location=$('#m_location').value.trim(),ip=$('#m_ip').value.trim(),url=$('#m_url').value.trim();
  if(!name||!url){$('#m_err').textContent='\u540d\u79f0\u548c URL \u5fc5\u586b';$('#m_err').style.display='block';return;}
  await api('source/update',{id,name,location,ip,url});closeModal();await refresh();toast('\u5df2\u4fdd\u5b58');
}
function srcDel(id){
  const refs=STATE.combos.filter(c=>c.sources.includes(id));
  const warn=refs.length?'<div class="warn">\u8be5\u6e90\u88ab\u4ee5\u4e0b\u7ec4\u5408\u5f15\u7528\uff0c\u5220\u9664\u540e\u5c06\u81ea\u52a8\u4ece\u8fd9\u4e9b\u7ec4\u5408\u6458\u9664\u5e76\u91cd\u751f\u6210\uff1a<br><b>'+refs.map(c=>esc(c.slug)).join('\u3001')+'</b><br>\u82e5\u6458\u9664\u540e\u67d0\u7ec4\u5408\u53d8\u7a7a\uff0c\u5176\u8ba2\u9605\u7aef\u70b9\u5c06\u8fd4\u56de\u7a7a\u8282\u70b9\u3002</div>':'';
  openModal('<h3>\u5220\u9664\u6e90 '+esc(id)+'</h3>'+warn+'<div class="acts"><button class="btn" onclick="closeModal()">取消</button><button class="btn danger" onclick="srcDelOk(\''+id+'\')">\u786e\u8ba4\u5220\u9664</button></div>');
}
async function srcDelOk(id){const sum=await api('source/delete',{id});closeModal();showFeedback(sum,'删除源 '+id);await refresh();}

function btnAddSrc(){
  openModal(`
    <h3>\u65b0\u589e\u6e90</h3>
    <div class="field"><label>\u8ba2\u9605 URL</label><input id="m_url" placeholder="https://sui.example.com/sub/..." oninput="autoFillFromUrl()"></div>
    <div class="field"><label>\u540d\u79f0</label><input id="m_name" placeholder="\u81ea\u52a8\u63d0\u53d6\uff0c\u53ef\u4fee\u6539"></div>
    <div class="field"><label>IP <button class="btn sm" onclick="resolveIpFromUrl()" style="margin-left:8px" id="btnRIp">\u4ece\u57df\u540d\u89e3\u6790</button></label><input id="m_ip"></div>
    <div class="field"><label>\u6240\u5728\u5730 <button class="btn sm" onclick="resolveLocFromIp()" style="margin-left:8px" id="btnRLoc">\u4eceIP\u83b7\u53d6</button></label><input id="m_location" placeholder="\u5982 \u9999\u6e2f\u3001\u6d1b\u6715\u77ed"></div>
    <div class="field err" id="m_err"></div>
    <div class="acts"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="srcAdd()">\u6dfb\u52a0\u5e76\u6293\u53d6</button></div>
  `);
}
// 从订阅URL自动提取：名称(大写) + IP(DNS) + 所在地(ip-api)
let _autoFilling=0;
async function autoFillFromUrl(){
  const url=$('#m_url')?.value?.trim();
  if(!url)return;
  // 1. 提取名称：从 sui.{name}.{domain} 或 sui.{name} 格式提取，转大写
  const m=url.match(/(?:\/|\/\/)sui[.\-]([a-zA-Z][a-zA-Z0-9\-]*)/);
  if(m){const n=m[1].toUpperCase();if(!$('#m_name').value)$('#m_name').value=n;}
  // 2. 自动解析IP和所在地（防抖）
  _autoFilling++;const tag=_autoFilling;
  // UI: 显示解析中状态
  const ipEl=$('#m_ip'), locEl=$('#m_location');
  const ipPh=ipEl?ipEl.placeholder:'', locPh=locEl?locEl.placeholder:'';
  if(ipEl)ipEl.placeholder='\u6b63\u5728\u89e3\u6790IP\u2026';
  if(locEl)locEl.placeholder='\u7b49\u5f85IP\u2026';
  await new Promise(r=>setTimeout(r,500));
  if(tag!==_autoFilling){if(ipEl)ipEl.placeholder=ipPh;if(locEl)locEl.placeholder=locPh;return;}
  // 解析IP
  if(ipEl)ipEl.placeholder='\u6b63\u5728\u89e3\u6790IP\u2026';
  try{
    const r=await api('source/resolve-ip',{url});
    if(r.ip&&ipEl)ipEl.value=r.ip;
  }catch(e){}
  if(tag!==_autoFilling)return;
  // 解析所在地
  const resolvedIp=ipEl?ipEl.value.trim():'';
  if(resolvedIp){
    if(locEl)locEl.placeholder='\u6b63\u5728\u67e5\u8be2\u4f4d\u7f6e\u2026';
    try{
      const r2=await api('source/resolve-location',{ip:resolvedIp});
      if(r2.location&&locEl)locEl.value=r2.location;
    }catch(e){}
  }
  // 恢复placeholder
  if(ipEl&&!ipEl.value)ipEl.placeholder=ipPh; else if(ipEl)ipEl.placeholder='';
  if(locEl&&!locEl.value)locEl.placeholder=locPh; else if(locEl)locEl.placeholder='';
}
// 新增源模式：手动触发从域名解析IP
async function resolveIpFromUrl(){
  const url=$('#m_url')?.value?.trim();if(!url){toast('\u8bf7\u5148\u586b\u8ba2\u9605URL');return;}
  const b=$('#btnRIp');if(b){b.disabled=true;b.textContent='\u89e3\u6790\u4e2d\u2026';}
  try{const r=await api('source/resolve-ip',{url});if(r.ip){$('#m_ip').value=r.ip;toast('IP: '+r.ip);}else toast(r.error||'\u89e3\u6790\u5931\u8d25');}catch(e){toast('\u89e3\u6790\u5931\u8d25');}
  if(b){b.disabled=false;b.textContent='\u4ece\u57df\u540d\u89e3\u6790';}
}
// 新增源模式：手动触发从IP查所在地
async function resolveLocFromIp(){
  const ip=$('#m_ip')?.value?.trim();if(!ip){toast('\u8bf7\u5148\u586bIP');return;}
  const b=$('#btnRLoc');if(b){b.disabled=true;b.textContent='\u67e5\u8be2\u4e2d\u2026';}
  try{const r=await api('source/resolve-location',{ip});if(r.location){$('#m_location').value=r.location;toast('\u6240\u5728\u5730: '+r.location);}else toast(r.error||'\u67e5\u8be2\u5931\u8d25');}catch(e){toast('\u67e5\u8be2\u5931\u8d25');}
  if(b){b.disabled=false;b.textContent='\u4eceIP\u83b7\u53d6';}
}
async function srcAdd(){
  const name=$('#m_name').value.trim(),location=$('#m_location').value.trim(),ip=$('#m_ip').value.trim(),url=$('#m_url').value.trim();
  if(!name||!url){$('#m_err').textContent='\u540d\u79f0 / URL \u5fc5\u586b';$('#m_err').style.display='block';return;}
  const sum=await api('source/add',{name,location,ip,url});
  if(sum.error){$('#m_err').textContent=sum.error;$('#m_err').style.display='block';return;}
  closeModal();showFeedback(sum,'\u65b0\u589e\u6e90 '+sum.id);await refresh();
}

// 组合操作
function comboEdit(slug){
  const c=STATE.combos.find(x=>x.slug===slug);
  openComboModal(c.slug,c.remark||'',c.sources);
}
function btnAddCombo(){openComboModal('','',[]);}
function openComboModal(slug,remark,sources){
  const srcs=STATE.sources.map(s=>`<label><span style="display:inline-block"><input type="checkbox" value="${esc(s.id)}" ${sources.includes(s.id)?'checked':''} ${s.enabled?'':'disabled'}></span> ${esc(s.name)}${s.location?' '+esc(s.location):''}</label>`).join('');
  const slugField = slug?`<input id="m_slug" value="${esc(slug)}" placeholder="\u5b57\u6bcd\u6570\u5b57">`:`<input id="m_slug" placeholder="\u5b57\u6bcd\u6570\u5b57">`;
  const slugHint = slug?'<div style="font-size:11px;color:var(--mut);margin-top:3px">\u4fee\u6539 Slug \u4f1a\u66f4\u65b0\u8ba2\u9605\u7aef\u70b9\u8def\u5f84\uff0c\u65e7\u94fe\u63a5\u5c07\u65e0\u6cd5\u8bbf\u95ee</div>':'';
  openModal(`
    <h3>${slug?'\u7f16\u8f91\u7ec4\u5408':'\u65b0\u5efa\u7ec4\u5408'}</h3>
    <div class="field"><label>Slug\uff08URL \u7528\uff0c\u552f\u4e00\uff0c\u5982 mix3\uff09</label>${slugField}${slugHint}</div>
    <div class="field"><label>\u5907\u6ce8\uff08\u5c55\u793a\u6807\u9898\uff0c\u7559\u7a7a\u663e\u793a\u300cslug\u7ec4\u5408\u300d\uff09</label><input id="m_remark" value="${esc(remark)}" placeholder="\u53ef\u9009"></div>
    <div class="field"><label>\u5f15\u7528\u6e90\uff08\u52fe\u9009\uff09</label><div class="srcpick">${srcs||'<span class="mut">\u6682\u65e0\u6e90</span>'}</div></div>
    <div class="field err" id="m_err"></div>
    <div class="acts"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="comboSave('${slug}')">\u4fdd\u5b58</button></div>
  `);
}
async function comboSave(origSlug){
  const slug=$('#m_slug').value.trim(),remark=$('#m_remark').value.trim();
  const sources=[...$$('#modal .srcpick input:checked')].map(x=>x.value);
  if(!slug){$('#m_err').textContent='Slug 必填';$('#m_err').style.display='block';return;}
  const payload={slug,remark,sources};
  if(origSlug) payload.orig_slug=origSlug;
  let sum;
  if(origSlug){sum=await api('combo/update',payload);}
  else{sum=await api('combo/add',payload);}
  if(sum.error){$('#m_err').textContent=sum.error;$('#m_err').style.display='block';return;}
  closeModal();await refresh();toast('\u5df2\u4fdd\u5b58');
}
function comboDel(slug){
  openModal('<h3>\u5220\u9664\u7ec4\u5408 '+esc(slug)+'</h3><div class="warn">\u5c06\u5220\u9664\u8be5\u7ec4\u5408\u7684\u8ba2\u9605\u7aef\u70b9\uff08'+esc(slug)+' / '+esc(slug)+'/clash / '+esc(slug)+'/singbox\uff09\u3002\u6e90\u4e0d\u53d7\u5f71\u54cd\u3002</div><div class="acts"><button class="btn" onclick="closeModal()">取消</button><button class="btn danger" onclick="comboDelOk(\''+slug+'\')">\u786e\u8ba4\u5220\u9664</button></div>');
}
async function comboDelOk(slug){await api('combo/delete',{slug});closeModal();await refresh();toast('\u5df2\u5220\u9664');}

async function srcResolveLocation(id){
  const btn=$('#btnRLoc');if(btn){btn.disabled=true;btn.textContent='\u67e5\u8be2\u4e2d\u2026';}
  try{
    const r=await api('source/resolve-location',{id});
    if(r.location){$('#m_location').value=r.location;toast('\u6240\u5728\u5730: '+r.location);}
    else toast(r.error||'\u67e5\u8be2\u5931\u8d25');
  }catch(e){toast('\u67e5\u8be2\u5931\u8d25');}
  if(btn){btn.disabled=false;btn.textContent='\u4eceIP\u83b7\u53d6';}
}
async function srcResolveIp(id){
  const s=STATE.sources.find(x=>x.id===id);
  if(!s||!s.url){toast('\u65e0\u8ba2\u9605URL');return;}
  const btn=$('#btnRIp');if(btn){btn.disabled=true;btn.textContent='\u89e3\u6790\u4e2d\u2026';}
  try{
    const r=await api('source/resolve-ip',{url:s.url});
    if(r.ip){$('#m_ip').value=r.ip;toast('IP: '+r.ip);}
    else toast(r.error||'\u89e3\u6790\u5931\u8d25');
  }catch(e){toast('\u89e3\u6790\u5931\u8d25');}
  if(btn){btn.disabled=false;btn.textContent='\u4ece\u57df\u540d\u89e3\u6790';}
}

// ══════════ 拖拽排序 ══════════
function bindDragDrop(){
  const tbody=$('#srcBody');
  $$('tbody tr[draggable]').forEach(tr=>{
    tr.addEventListener('dragstart', e=>{
      draggedRow=tr;
      tr.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain',tr.dataset.id);
    });
    tr.addEventListener('dragend',()=>{
      tr.classList.remove('dragging');
      $$('tr.drag-over').forEach(r=>r.classList.remove('drag-over'));
      draggedRow=null;
    });
    tr.addEventListener('dragover', e=>{
      e.preventDefault();
      if(draggedRow && draggedRow!==tr){
        $$('tr.drag-over').forEach(r=>r.classList.remove('drag-over'));
        tr.classList.add('drag-over');
      }
    });
    tr.addEventListener('drop', async e=>{
      e.preventDefault();
      tr.classList.remove('drag-over');
      if(!draggedRow || draggedRow===tr) return;
      const fromId=draggedRow.dataset.id;
      const toId=tr.dataset.id;
      try{
        await api('source/reorder',{from_id:fromId,to_id:toId});
        await refresh();
        toast('\u6392\u5e8f\u5df2\u4fdd\u5b58');
      }catch(err){}
    });
  });
}

// 弹窗 / 日志
function openModal(html){$('#modal').innerHTML=html;$('#mask').classList.add('show');}
function closeModal(){$('#mask').classList.remove('show');}
$('#mask').onclick=(e)=>{if(e.target.id==='mask')closeModal();};
// 日志面板——结构化渲染
function openLog(text){
  if(!text || !text.trim()){
    openModal('<h3>合并日志</h3><div class="empty" style="display:block;text-align:center;padding:40px 0;color:#9ca3af">暂无日志记录</div><div class="acts"><button class="btn primary" onclick="closeModal()">关闭</button></div>');
    return;
  }
  const lines=text.trim().split('\n');
  const groups=[];let cur=null;
  for(const raw of lines){
    const line=raw.trim();if(!line)continue;
    // 新操作组：以时间戳开头
    const tm=line.match(/^(\[\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}\])/);
    if(tm){
      if(cur){groups.push(cur);}
      cur={time:tm[1],lines:[],dur:'',summary:''};
      const rest=line.slice(tm[1].length).trim();
      if(rest){cur.lines.push({t:'ok',text:rest});}
      continue;
    }
    if(!cur){cur={time:'',lines:[],dur:'',summary:''};}
    // 耗时行
    if(line.startsWith('[=] 耗时')){cur.dur=line.replace('[=] ','');cur.lines.push({t:'info',text:line.replace('[=] ','')});continue;}
    // 分类每行
    if(line.startsWith('[OK]')) cur.lines.push({t:'ok',text:line.replace('[OK] ','')});
    else if(line.startsWith('[')) cur.lines.push({t:'sys',text:line});
    else cur.lines.push({t:'info',text:line});
  }
  if(cur && (cur.lines.length||cur.time)) groups.push(cur);

  // 渲染（最多显示最近20组）
  let html='<div class="log-panel">';
  const show=groups.length>20?groups.slice(-20):groups;
  for(const g of show){
    html+='<div class="log-group">';
    html+='<div class="log-group-head">';
    html+='<span class="time">'+esc(g.time)+' '+esc(g.lines.find(l=>l.text.includes('完成'))?.text||'')+'</span>';
    if(g.dur){html+='<span class="badge">'+esc(g.dur)+'</span>';}
    html+='</div>';
    html+='<div class="log-group-body">';
    for(const l of g.lines){
      const tag=l.t==='ok'?'OK':l.t==='sys'?'SYS':l.t==='err'?'ERR':'';
      html+='<div class="log-line">';
      if(tag){html+='<span class="log-tag '+l.t+'">'+tag+'</span>';}

      // 对抓取结果行做高亮处理
      let txt=esc(l.text);
      if(l.t==='ok' && l.text.includes(':')){
        const p=l.text.split(':');
        if(p.length>=2){
          txt='<b>'+esc(p[0].trim())+'</b>:<span class="dim">'+esc(p.slice(1).join(':').trim())+'</span>';
        }
      }else if(l.t==='info' && l.text.includes('组合')){
        txt=txt.replace(/(重新组合)/,'<span class="hl">$1</span>');
        txt=txt.replace(/(无)/,'<span class="dim">$1</span>');
      }
      html+='<span class="log-text">'+txt+'</span>';
      html+='</div>';
    }
    html+='</div></div>';
  }
  if(groups.length>20){html+='<div style="text-align:center;color:#9ca3af;font-size:12px;padding:8px 0">仅显示最近 '+show.length+' 条记录（共 '+groups.length+' 条）</div>';}
  html+='</div>';
  openModal('<h3>合并日志</h3>'+html+'<div class="acts"><button class="btn primary" onclick="closeModal()">关闭</button></div>');
}

// 启动
(async()=>{
  try{const ck=await api('check');if(!ck.ok){showLogin();return;}}
  catch(e){showLogin();return;}
  await refresh();
})();

function showLogin(){
  document.body.innerHTML='<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:40px 20px;background:#f5f6f8">'+
    '<div class="card" style="width:360px;max-width:92vw;text-align:center;padding:32px 24px">'+
    '<h1 style="margin:0 0 6px;font-size:22px;color:var(--txt)">ZSUB 订阅管理</h1>'+
    '<p style="margin:0 0 20px;color:var(--mut);font-size:13px">请输入密码登录</p>'+
    '<div class="field"><label>密码</label><input id="pw" type="password" onkeydown="if(event.key===\'Enter\')doLogin()"></div>'+
    '<div class="field err" id="lerr"></div>'+
    '<button class="btn primary" style="width:100%;padding:11px 0;font-size:15px" onclick="doLogin()">登录</button></div></div>';
}
async function doLogin(){
  const pw=$('#pw').value;
  const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const j=await r.json();
  if(j.ok){location.reload();}
  else{$('#lerr').textContent='密码错误';$('#lerr').style.display='block';}
}
</script>
</body>
</html>'''


# ──── IP 质量检测（GeoIP + DNSBL，带加权 LRU 缓存）────
# 设计：缓存上限 100 个 IP；权重 = 查询次数，查询越多保留越久；
# 满 100 时淘汰「权重最低，权重相同则最久未查询」的 IP；
# 缓存命中直接返回（不联网），仅缓存缺失自动请求，后续靠用户手动刷新。
IPQ_CACHE_FILE = '/opt/sub-converter/ip_quality_cache.json'
IPQ_MAX = 100
IPQ_LOCK = threading.Lock()
IPQ_UA = 'ZSUB-Admin/1.0'

# DNSBL 清单（纯 DNS 查询，免 key）。命中返回 127.0.0.x，未命中抛 NXDOMAIN
DNSBL_ZONES = [
    ('Spamhaus Zen', 'zen.spamhaus.org'),
    ('SpamCop', 'bl.spamcop.net'),
    ('SORBS', 'dnsbl.sorbs.net'),
    ('CBL', 'cbl.abuseat.org'),
    ('DroneBL', 'dnsbl.dronebl.org'),
    ('PSBL', 'psbl.surriel.com'),
    ('UCEPROTECT', 'dnsbl.uceprotect.net'),
    ('Barracuda', 'b.barracudacentral.org'),
    ('Tor Exit', 'tor.dan.me.uk'),
]

def ipq_load():
    try:
        with open(IPQ_CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def ipq_save(cache):
    try:
        tmp = IPQ_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, IPQ_CACHE_FILE)
    except Exception:
        pass

def ipq_evict(cache):
    # 淘汰权重(查询次数)最低者；权重相同则 last_queried 最小(最久未用)者
    victim = min(cache.items(), key=lambda kv: (kv[1].get('count', 0), kv[1].get('last_queried', 0)))
    cache.pop(victim[0], None)

def geoip_query(ip):
    try:
        fields = ('status,message,country,countryCode,regionName,city,district,zip,'
                  'lat,lon,timezone,offset,currency,isp,org,as,asname,mobile,proxy,hosting,reverse,query')
        url = 'http://ip-api.com/json/%s?lang=zh-CN&fields=%s' % (ip, fields)
        req = urllib.request.Request(url, headers={'User-Agent': IPQ_UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return {'status': 'fail', 'message': str(e)}

def dnsbl_query(ip):
    if ':' in ip:  # IPv6 多数清单仅支持 v4，跳过
        return {'hits': 0, 'total': len(DNSBL_ZONES), 'lists': [], 'skipped': True}
    rev = '.'.join(reversed(ip.split('.')))
    results = []
    hits = 0
    prev_to = socket.getdefaulttimeout()
    socket.setdefaulttimeout(4)
    try:
        for name, zone in DNSBL_ZONES:
            try:
                ans = socket.gethostbyname('%s.%s' % (rev, zone))
                hits += 1
                results.append({'name': name, 'code': ans})
            except Exception:
                results.append({'name': name, 'code': None})
    finally:
        socket.setdefaulttimeout(prev_to)
    return {'hits': hits, 'total': len(DNSBL_ZONES), 'lists': [x for x in results if x.get('code')]}

def derive_judgment(geo, dnsbl):
    if geo.get('proxy'):
        ip_type = '代理/VPN'
    elif geo.get('mobile'):
        ip_type = '移动网络'
    elif geo.get('hosting'):
        ip_type = '机房(IP)'
    else:
        ip_type = '住宅(IP)'
    hits = dnsbl.get('hits', 0)
    reputation = '干净' if hits == 0 else ('可疑' if hits <= 2 else '滥用')
    return ip_type, reputation

def detect_ip(ip):
    geo = geoip_query(ip)
    dnsbl = dnsbl_query(ip)
    ip_type, reputation = derive_judgment(geo, dnsbl)
    return {
        'ip': ip,
        'geo': geo,
        'dnsbl': dnsbl,
        'ip_type': ip_type,
        'reputation': reputation,
        'blacklist_hits': dnsbl.get('hits', 0),
        'detected_at': time.time(),
    }

def get_ip_quality(ip, force=False):
    ip = (ip or '').strip()
    if not ip:
        return {'ok': False, 'error': '缺少 IP'}
    now = time.time()
    with IPQ_LOCK:
        cache = ipq_load()
        entry = cache.get(ip)
        if entry and not force:
            entry['count'] = entry.get('count', 0) + 1
            entry['last_queried'] = now
            ipq_save(cache)
            d = dict(entry['data'])
            d['cached'] = True
            d['count'] = entry['count']
            return {'ok': True, **d}
        data = detect_ip(ip)
        if entry:
            entry['data'] = data
            entry['cached_at'] = now
            entry['last_queried'] = now
            entry['count'] = entry.get('count', 0) + 1
        else:
            if len(cache) >= IPQ_MAX:
                ipq_evict(cache)
            cache[ip] = {'count': 1, 'cached_at': now, 'last_queried': now, 'data': data}
        ipq_save(cache)
        d = dict(data)
        d['cached'] = False
        d['count'] = cache[ip]['count']
        return {'ok': True, **d}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # 防浏览器/CDN 缓存旧版 HTML
        if ctype.startswith('text/html'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            ln = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(ln) if ln else b''
            return json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/admin', '/admin/'):
            self._send(200, HTML_PAGE, 'text/html; charset=utf-8')
            return
        if p == '/admin/api/check':
            self._send(200, {'ok': check_session(self)})
            return
        if p == '/admin/api/state':
            if not check_session(self):
                self._send(401, {'error': 'unauth'}); return
            self._send(200, state_payload())
            return
        if p == '/admin/api/log':
            if not check_session(self):
                self._send(401, {'error': 'unauth'}); return
            try:
                with open('/opt/sub-converter/merge.log') as f:
                    log = ''.join(f.readlines()[-60:])
            except Exception:
                log = ''
            self._send(200, {'log': log})
            return
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        p = self.path.split('?')[0]
        if p == '/admin/api/login':
            d = self._read_json()
            if d.get('password') == PASSWORD:
                tok = gen_token()
                SESSIONS[tok] = True
                self._send(200, {'ok': True}, extra={
                    'Set-Cookie': f'zsm_session={tok}; Path=/admin/; HttpOnly; SameSite=Lax'})
            else:
                self._send(200, {'ok': False})
            return
        if p == '/admin/api/logout':
            ck = self.headers.get('Cookie', '')
            for part in ck.split(';'):
                part = part.strip()
                if part.startswith('zsm_session='):
                    SESSIONS.pop(part.split('=', 1)[1], None)
            self._send(200, {'ok': True})
            return
        if not check_session(self):
            self._send(401, {'error': 'unauth'}); return

        d = self._read_json()
        cfg = convert.load_config()

        if p == '/admin/api/preview':
            url = str(d.get('url', '')).strip()
            if not url:
                self._send(400, {'error': '缺少 URL'}); return
            from urllib.parse import urlparse
            import urllib.request
            import base64
            parsed = urlparse(url)
            if DOMAIN not in (parsed.hostname or ''):
                self._send(403, {'error': '不允许的域名'}); return
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'ZSUB-Admin/1.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode('utf-8', errors='replace')
                # 判断格式：base64 端点的订阅内容是 base64 密文，需解码为明文节点 URI
                low = url.rstrip('/')
                if low.endswith('/clash'):
                    fmt = 'clash'
                elif low.endswith('/singbox'):
                    fmt = 'singbox'
                else:
                    fmt = 'base64'
                if fmt == 'base64':
                    try:
                        b = content.strip().replace('-', '+').replace('_', '/')
                        b += '=' * (-len(b) % 4)
                        content = base64.b64decode(b).decode('utf-8', errors='replace')
                    except Exception:
                        pass  # 解码失败则原样返回（极端情况）
                if len(content) > 200000:
                    content = content[:200000] + '\n... (内容过长已截断)'
                self._send(200, {'content': content, 'size': len(content), 'fmt': fmt})
            except Exception as ex:
                self._send(502, {'error': f'获取失败: {str(ex)}'})
            return

        if p == '/admin/api/ip-quality':
            if not check_session(self):
                self._send(401, {'error': 'unauth'}); return
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                body = {}
            ip = (body.get('ip') or '').strip()
            force = bool(body.get('force', False))
            if not ip:
                sid = (body.get('id') or '').strip()
                if sid:
                    try:
                        cfg = convert.load_config()
                        src = next((s for s in cfg['sources'] if str(s['id']) == str(sid)), None)
                        if src:
                            ip = (src.get('ip') or '').strip()
                    except Exception:
                        pass
            res = get_ip_quality(ip, force)
            self._send(200, res)
            return
        if p == '/admin/api/update-all':
            self._send(200, convert.update_all()); return
        if p == '/admin/api/source/fetch':
            res = convert.update_source(d.get('id'))
            self._send(200, res); return
        if p == '/admin/api/source/toggle':
            src = convert.find_source(cfg, d.get('id'))
            if not src:
                self._send(400, {'error': '源不存在'}); return
            src['enabled'] = bool(d.get('enabled'))
            convert.save_config(cfg)
            if src['enabled'] and not src.get('content_hash'):
                convert.ensure_source_fetch_on_add(src['id'])
            self._send(200, {'ok': True}); return
        if p == '/admin/api/source/add':
            sid = str(d.get('id', '')).strip()
            # 前端不传ID时，自动分配数字ID
            if not sid:
                used_ids = [s['id'] for s in cfg['sources']]
                n = 1
                while str(n) in used_ids:
                    n += 1
                sid = str(n)
            if convert.find_source(cfg, sid):
                self._send(400, {'error': 'ID 无效或已存在'}); return
            # 名称重复检测
            new_name = d.get('name', '').strip()
            dup = [s['id'] for s in cfg['sources'] if s.get('name','').strip().lower() == new_name.lower()]
            if dup:
                self._send(400, {'error': f'名称「{new_name}」已存在（源 {dup[0]}），请换一个名称'}); return
            if not d.get('name') or not d.get('url'):
                self._send(400, {'error': '名称和 URL 必填'}); return
            news = {'id': sid, 'name': d['name'].strip(), 'ip': d.get('ip', '').strip(),
                    'location': d.get('location', '').strip(),
                    'url': d['url'].strip(), 'enabled': True, 'status': 'unknown',
                    'last_fetch': None, 'last_change': None, 'content_hash': '',
                    'node_count': 0, 'error': None, 'ip_quality': None}
            cfg['sources'].append(news)
            convert.save_config(cfg)
            sumry = convert.ensure_source_fetch_on_add(sid)
            resp = sumry or {'ok': True}
            if 'id' not in resp:
                resp['id'] = sid
            self._send(200, resp); return
        if p == '/admin/api/source/update':
            src = convert.find_source(cfg, d.get('id'))
            if not src:
                self._send(400, {'error': '源不存在'}); return
            # 名称重复检测（排除自身）
            if d.get('name'):
                new_name = d['name'].strip()
                dup = [s['id'] for s in cfg['sources'] if s['id'] != src['id'] and s.get('name','').strip().lower() == new_name.lower()]
                if dup:
                    self._send(400, {'error': f'名称「{new_name}」已被 {dup[0]} 使用'}); return
                src['name'] = new_name
            if 'ip' in d: src['ip'] = d['ip'].strip()
            if 'location' in d: src['location'] = d['location'].strip()
            if d.get('url'): src['url'] = d['url'].strip()
            convert.save_config(cfg)
            res = convert.update_source(src['id'])
            self._send(200, res); return
        if p == '/admin/api/source/delete':
            res = convert.delete_source(d.get('id'))
            self._send(200, res); return
        if p == '/admin/api/source/reorder':
            from_id = d.get('from_id', '')
            to_id = d.get('to_id', '')
            ids = [s['id'] for s in cfg['sources']]
            if from_id not in ids or to_id not in ids:
                self._send(400, {'error': '源ID无效'}); return
            fi, ti = ids.index(from_id), ids.index(to_id)
            item = ids.pop(fi)
            ids.insert(ti, item)
            # 按 ids 新顺序重排 sources 列表
            ordered = []
            for sid in ids:
                s = convert.find_source(cfg, sid)
                if s:
                    ordered.append(s)
            cfg['sources'] = ordered
            convert.save_config(cfg)
            self._send(200, {'ok': True}); return
        if p == '/admin/api/source/resolve-location':
            sid = d.get('id', '')
            direct_ip = d.get('ip', '').strip()
            src_ip = None
            if direct_ip:
                src_ip = direct_ip
            elif sid:
                src = convert.find_source(cfg, sid)
                if not src:
                    self._send(200, {'error': '源不存在'}); return
                src_ip = src.get('ip','').strip()
            if not src_ip:
                self._send(200, {'error': '无IP，请先解析IP地址'}); return
            try:
                import urllib.request
                req = urllib.request.urlopen('http://ip-api.com/json/' + src_ip + '?fields=status,message,city,regionName,country,isp&lang=zh-CN', timeout=8)
                info = json.loads(req.read().decode())
                if info.get('status') == 'success':
                    parts = [info.get('regionName',''), info.get('country','')]
                    loc = ' '.join(x for x in parts if x).strip()
                    if not loc: loc = info.get('country','')
                    if not loc: loc = info.get('city','')
                    self._send(200, {'location': loc})
                else:
                    self._send(200, {'error': info.get('message', '查询失败')})
            except Exception as e:
                self._send(200, {'error': str(e)}); return
        if p == '/admin/api/source/resolve-ip':
            url = d.get('url', '')
            try:
                from urllib.parse import urlparse
                hostname = urlparse(url).hostname
                if not hostname:
                    self._send(200, {'error': '无法提取域名'}); return
                ip = socket.gethostbyname(hostname)
                self._send(200, {'ip': ip})
            except Exception as e:
                self._send(200, {'error': f'DNS解析失败: {e}'}); return
        if p == '/admin/api/combo/add':
            slug = str(d.get('slug', '')).strip()
            if not slug or convert.find_combo(cfg, slug):
                self._send(400, {'error': 'Slug 无效或已存在'}); return
            sources = [s for s in d.get('sources', []) if convert.find_source(cfg, s)]
            nc = {'slug': slug, 'remark': d.get('remark', '').strip(), 'sources': sources,
                  'node_count': 0, 'updated_at': None, 'content_hash': ''}
            cfg['combos'].append(nc)
            convert.save_config(cfg)
            convert.regenerate_combo(cfg, nc, save=True)
            try:
                convert.write_nginx_config()
            except Exception as e:
                self._send(200, {'ok': True, 'nginx_error': str(e)}); return
            self._send(200, {'ok': True}); return
        if p == '/admin/api/combo/update':
            orig_slug = d.get('orig_slug', '')
            new_slug = str(d.get('slug', '')).strip()
            if not new_slug:
                self._send(400, {'error': 'Slug 必填'}); return
            if orig_slug:
                combo = convert.find_combo(cfg, orig_slug)
            else:
                combo = None
            if not combo:
                self._send(400, {'error': '组合不存在'}); return
            # slug 变更时检查唯一性
            if new_slug != orig_slug and convert.find_combo(cfg, new_slug):
                self._send(400, {'error': f'Slug "{new_slug}" 已被使用'}); return
            # 执行 slug 重命名
            if new_slug != orig_slug:
                old_slug = combo['slug']
                combo['slug'] = new_slug
            if 'remark' in d: combo['remark'] = d['remark'].strip()
            if 'sources' in d:
                combo['sources'] = [s for s in d['sources'] if convert.find_source(cfg, s)]
            convert.save_config(cfg)
            convert.regenerate_combo(cfg, combo, save=True)
            try:
                convert.write_nginx_config()
            except Exception as e:
                self._send(200, {'ok': True, 'nginx_error': str(e)}); return
            self._send(200, {'ok': True}); return
        if p == '/admin/api/combo/delete':
            res = convert.delete_combo(d.get('slug'))
            try:
                convert.write_nginx_config()
            except Exception as e:
                res['nginx_error'] = str(e)
            self._send(200, res); return
        self._send(404, {'error': 'not found'})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 8088), Handler)
    server.serve_forever()
