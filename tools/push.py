#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开发用：把本地改动的代码推送到目标 VPS（需已部署好 /opt/sub-converter）。
SSH 连接信息从本地 tools/.env 读取（不在仓库内，已被 .gitignore 忽略），
格式：
    SSH_HOST=1.2.3.4
    SSH_PORT=22
    SSH_USER=root
    SSH_PASS=your-ssh-password
或通过这些环境变量传入。切勿把真实凭据写进仓库。
用法: python3 tools/push.py
"""
import os, sys, paramiko

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(ENV):
    for line in open(ENV, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ.get('SSH_HOST')
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ.get('SSH_USER', 'root')
PASS = os.environ.get('SSH_PASS')

if not HOST or not PASS:
    print('✗ 请在 tools/.env 中配置 SSH_HOST / SSH_PASS（或用环境变量传入）')
    sys.exit(1)

FILES = [
    ('server.py', '/opt/sub-converter/server.py'),
    ('convert.py', '/opt/sub-converter/convert.py'),
    ('merge_sub.sh', '/opt/sub-converter/merge_sub.sh'),
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
print('connecting...')
ssh.connect(HOST, port=PORT, username=USER, password=PASS,
            allow_agent=False, look_for_keys=False, timeout=30)
sftp = ssh.open_sftp()
for local, remote in FILES:
    sftp.put(os.path.join(BASE, local), remote)
    print('PUT', local, '->', remote)
sftp.close()
ssh.exec_command('chmod +x /opt/sub-converter/merge_sub.sh')
print('uploaded.')
ssh.close()
print('DONE （请在目标 VPS 上：systemctl restart zsm 并跑 merge_sub.sh）')
