#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地部署前校验：
  1) Python 语法校验 (server.py, convert.py)
  2) 提取 server.py 内联 <script> 跑 node --check (JS 语法校验)
用法: python3 tools/check_local.py
依赖: node (在 PATH 中)
"""
import re, subprocess, sys, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def py_ok(fn):
    p = os.path.join(BASE, fn)
    r = subprocess.run([sys.executable, '-c',
                        f'import ast,sys; ast.parse(open(r"{p}",encoding="utf-8").read())'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('PYTHON SYNTAX FAIL', fn); print(r.stderr); return False
    print('PYTHON OK  ', fn); return True


def js_ok():
    src = open(os.path.join(BASE, 'server.py'), encoding='utf-8').read()
    m = re.search(r'<script>(.*?)</script>', src, re.S)
    if not m:
        print('NO SCRIPT BLOCK FOUND'); return False
    tmp = os.path.join(BASE, '_check.js')
    open(tmp, 'w', encoding='utf-8').write(m.group(1))
    node = 'node'  # 依赖 PATH 中的 node
    r = subprocess.run([node, '--check', tmp], capture_output=True, text=True)
    if r.returncode != 0:
        print('JS SYNTAX FAIL'); print(r.stderr); return False
    print('JS OK      server.py inline script'); return True


if not py_ok('server.py') or not py_ok('convert.py') or not js_ok():
    sys.exit(1)
print('ALL CHECKS PASSED')
