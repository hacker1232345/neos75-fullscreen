#!/usr/bin/env python3
"""Issue 里贴一份固件备份 → 自动算出一份可直接刷的镜像。

这是「一劳永逸适配」的对外入口：用户不用会任何命令，只要在 GitHub 上开一个
issue、把键盘导出的固件备份拖进去，机器人就能给出

  * 这份固件能套哪几层（人话结论）
  * 一份打好补丁、补好 DFU 校验的完整镜像
  * 一份派生 profile（以后别人拿同样的固件就不用再算了）

**这个脚本永远不碰设备**：不刷写、不需要 USB，只在内存里算字节。刷不刷由用户
自己在本地决定。

用法
----
  python tools/adapt_issue.py --url <附件链接> --out ci/
  python tools/adapt_issue.py --file backup.bin --out ci/
  python tools/adapt_issue.py --text "<issue 正文>" --out ci/   # 从正文里挑链接
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

PAYLOAD_BYTES = 491520
TOTAL_BYTES = PAYLOAD_BYTES + 16
MAX_DOWNLOAD = 8 * 1024 * 1024          # 固件不到 0.5 MB，8 MB 是防呆上限

# GitHub 把 issue 里的附件改写成 https://github.com/user-attachments/assets/...；
# 其它常见图床/cdn 也一并认，用户爱传哪儿就传哪儿。
URL_RE = re.compile(r'https?://[^\s<>()"\'`]+\.(?:bin|BIN)(?:\?[^\s<>()"\'`]*)?')
ANY_RE = re.compile(r'https?://[^\s<>()"\'`]+')


def find_url(text):
    """从 issue 正文里挑出最像固件备份的那个链接。"""
    if not text:
        return None
    m = URL_RE.search(text)
    if m:
        return m.group(0)
    for m in ANY_RE.finditer(text):
        u = m.group(0)
        if 'user-attachments' in u or '/assets/' in u:
            return u
    return None


def download(url, dest):
    """把附件抓下来。GitHub 的 user-attachments 可以直接匿名 GET。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'neos75-adapt/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f'附件超过 {MAX_DOWNLOAD} 字节，看起来不是固件备份')
    Path(dest).write_bytes(data)
    return Path(dest)


def grab(url, out):
    """抓附件，失败只说不成功、不抛栈 —— 这是给 issue 里的普通用户看的。"""
    try:
        return download(url, out / 'input.bin')
    except Exception as e:
        print(f'附件抓不下来：{e}\n'
              f'（链接：{url}）\n'
              f'如果是 GitHub 的 user-attachments 偶尔会 404，把备份重新拖一次再试。')
        return None


def check(data):
    """先判「这到底是不是一份键盘固件备份」，不是就早退，别浪费后面几十秒。"""
    n = len(data)
    if n not in (PAYLOAD_BYTES, TOTAL_BYTES):
        raise ValueError(f'长度是 {n} 字节，键盘备份应该是 {PAYLOAD_BYTES} 或 {TOTAL_BYTES} 字节。'
                         f'（屏幕固件、UI 资源包、接收器固件都不是这个长度）')
    payload = data[:PAYLOAD_BYTES]
    code = payload[:487424]
    if code.count(0xFF) > len(code) * 0.5:
        raise ValueError('备份里超过一半是空白（0xFF），像是没读到真 flash。'
                         '请在程序里重新备份一次再传。')
    return payload


def run(adapt, payload, name, out):
    """跑适配，产出镜像 / 派生 profile / 结论。返回 (markdown, exit_code)。"""
    rep = adapt.analyse(payload, name)
    lines = [f'### 这份固件（{name}，{len(payload)} 字节）', '']
    lines += ['```', adapt.explain(rep).rstrip(), '```', '']

    try:
        image, rep2, chain, meta = adapt.build_adapted(payload, name)
    except ValueError as e:
        lines += [f'**没法自动适配**：{e}', '', '补丁点对不上，这份固件的布局和已知版本差异太大。'
                  '把备份留在 issue 里，我会人工看一遍。']
        return '\n'.join(lines), 5

    if not image:
        lines += ['**不需要打任何补丁**：这份固件已经是能套到的最新状态。', '']
        return '\n'.join(lines), 4

    out.mkdir(parents=True, exist_ok=True)
    fw = out / f'adapted-{hashlib.sha256(payload[:487424]).hexdigest()[:8]}.bin'
    fw.write_bytes(image)
    lines += [f'**可刷镜像**：`{fw.name}`，{len(image)} 字节，'
              f'sha256 `{hashlib.sha256(image).hexdigest()}`', '',
              f'套用了 {meta["appliedProfiles"]}，共改 {meta["changed_bytes"]} 字节。', '']

    # 派生 profile 也一起交出去：别人拿同样的固件就不用再算一遍
    for L in rep2['layers']:
        if L['verdict'] not in ('ready', 'partial'):
            continue
        d = L['derived']
        if L['verdict'] == 'partial':
            d['validation'] = 'content-anchored-partial'
        p = out / f'{d["profile"]}-{L["profile"]}.patch.json'
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        lines.append(f'派生出 `{p.name}`（以后同样的固件直接用它，不用再算）')
    lines += ['', '刷写请在本机做，**下载后先核对 sha256**：', '',
              '```', 'python desktop/app.py --backup my-backup.bin', '# 先备份再刷',
              f'python desktop/app.py --maintain flash --baseline my-backup.bin '
              f'--target {fw.name}', '```', '',
              '刷坏了怎么办：屏幕主控在 `0x08000000` 有 DFU 引导器，键盘上的'
              '`fn + 空格` 能回 DFU；实在起不来就按 `docs/找BOOT0指引_20261102.md` 接 SWD。']
    return '\n'.join(lines), 0


def main():
    ap = argparse.ArgumentParser(description='把 issue 里的固件备份自动适配成可刷镜像')
    ap.add_argument('--url')
    ap.add_argument('--file')
    ap.add_argument('--text', help='issue 正文，从里面挑附件链接')
    ap.add_argument('--out', default='ci')
    ap.add_argument('--name')
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    if a.file:
        src = Path(a.file)
    elif a.url:
        src = grab(a.url, out)
        if not src:
            return 2
    elif a.text:
        u = find_url(a.text)
        if not u:
            print('issue 正文里没找到固件附件的链接。'
                  '请把备份文件直接拖进 issue（.bin 文件），别只贴文字。')
            return 2
        print(f'从正文里挑到 {u}')
        src = grab(u, out)
        if not src:
            return 2
    else:
        ap.error('需要 --file / --url / --text 之一')
        return 2

    try:
        payload = check(src.read_bytes())
    except ValueError as e:
        print(f'这份附件不能用：{e}')
        return 3

    import adapt_firmware as adapt
    md, code = run(adapt, payload, a.name or src.name, out)
    (out / 'summary.md').write_text(md + '\n', encoding='utf-8')
    print(md)
    files = sorted(p.name for p in out.iterdir() if p.name != 'input.bin')
    print(f'\n产出：{", ".join(files)}')
    return code


if __name__ == '__main__':
    sys.exit(main())
