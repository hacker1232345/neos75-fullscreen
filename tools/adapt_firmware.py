"""一次性的固件适配：拿一份陌生备份，自动算出每一层补丁该往哪打。

这个工具存在的理由
------------------
每来一份新固件（同款键盘的不同批次/版本/语言），原来的做法是人工重做一遍 profile：
新固件算哈希 → 没命中 → 只能「强制模式」盲套链首 → 进度条去不掉 + 组件层不敢碰。
一份固件半小时起，而且每份都要重犯同样的错。

现在换成**内容锚点**：每条补丁在源镜像里有一小段「全片唯一」的上下文（`firmware/anchors/*.json`），
新固件里只要这段还在，就能搜到唯一位置，偏移是多少无所谓。锚点不包含被改的那几个字节本身
（有些补丁的旧字节在原片里出现几十万次，根本不能当线索）。

哪些层能自动、哪些不能
--------------------
* 就地改写（改代码/表项里的现有字节）→ 能自动，锚点搜到就算出新偏移。
* 注入填充区（往全 0xFF 的空闲带里塞一整段新代码）→ 走另一条路：**重新汇编**。
  那段代码内部写着 `bl 0x2637c`（厂商的 alloc）、`bl 0x2625c`（label 设文本）这类
  绝对地址，还有 `b.w 0x356e8`（命令处理完的返回点）和命令表项里的一格相对偏移。
  换一份固件，这些地址会一起漂，按字节搬 = 调用不相干的地址、屏幕直接跑飞。
  所以先做符号解析：每个厂商函数取它入口的 16 字节内容当签名，在新固件里搜唯一
  命中 —— 地址变了无所谓，内容不变。再用 `build_widget.relocate()` 把模板里的
  符号全换成新值，重新汇编一遍，放进新固件里最大的一块 0xFF 空闲带。

  这条路的前提是代码**没被改过**：签名必须能在新固件里原样找到。找不到就老实报
  `unresolved`，绝不硬搬。

用法
----
  python tools/adapt_firmware.py --firmware backup.bin              # 只看结论
  python tools/adapt_firmware.py --firmware backup.bin --json       # 完整报告
  python tools/adapt_firmware.py --firmware backup.bin --emit out/  # 写派生 profile
  python tools/adapt_firmware.py --selftest                        # 三级自检
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_DIR = ROOT / 'firmware' / 'anchors'
CODE_BYTES = 487424
PAYLOAD_BYTES = 491520
sys.path.insert(0, str(Path(__file__).resolve().parent))


def layers():
    out = []
    for d in (ROOT / 'firmware', ROOT / 'firmware' / 'profiles'):
        for p in sorted(d.glob('*.patch.json')):
            out.append(json.loads(p.read_text(encoding='utf-8')))
    return out


def anchors_for(profile):
    p = ANCHOR_DIR / f'{profile}.json'
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding='utf-8'))


def counts(img, pat):
    n, i = 0, img.find(pat)
    while i >= 0:
        n += 1
        i = img.find(pat, i + 1)
    return n


# ---------------------------------------------------------------- 锚点定位

def locate(img, anchor):
    """按锚点找补丁点的新偏移。返回 (offset|None, 原因)。"""
    blob = bytes.fromhex(anchor['anchor'])
    delta = anchor['anchor_delta']
    hits = []
    i = img.find(blob)
    while i >= 0:
        hits.append(i)
        i = img.find(blob, i + 1)
    if not hits:
        return None, f'锚点那 {len(blob)} 字节在新固件里找不到'
    if len(hits) > 1:
        return None, f'锚点在新固件里出现 {len(hits)} 次（不再唯一）'
    return hits[0] - delta, None


def is_injection_layer(layer):
    """这一层是不是「往空闲带里塞代码」。

    判据：某条补丁的 before 整段都是 0xFF（说明那地方原来是空白，打完就有了新代码），
    且长度 ≥ 32 字节 —— 单字节 `ff`→`00` 那种是就地改写立即数，不算。
    """
    for p in layer['patches']:
        raw = bytes.fromhex(p['before'])
        if len(raw) >= 32 and set(raw) == {0xFF}:
            return True
    return False


def layer_pages(layer):
    """这一层想要的页掩码，换算成页码列表。

    `pages` 有就用；没有就看 `page_mask`（0x3f = 六页全开）；两者都没有就是全开，
    因为最老的那版 profile（widget-dce6dce7）压根没写这个字段，它当时就是全开。
    """
    if 'pages' in layer:
        return list(layer['pages'] or [])
    mask = layer.get('page_mask')
    if mask is None:
        return [1, 2, 3, 4, 5, 6]
    m = int(str(mask), 16) if isinstance(mask, str) else int(mask)
    return [p for p in range(1, 7) if m >> (p - 1) & 1]


def is_mask_override(parent, layer):
    """这一层是不是「只改上一层的注入结果」——典型就是 widget 全开 → 默认关闭。

    判据：父层是注入层，而这一层没有任何补丁落在全 0xFF 的区域（它改的是父层刚
    写进去的代码），并且它声明了 pages/page_mask。这种层不用单独适配：把父层按
    它的页掩码重新汇编一遍，结果就等价于「父层 + 这一层」。
    """
    if not parent or not is_injection_layer(parent):
        return False
    if any(len(bytes.fromhex(p['before'])) >= 32 and set(bytes.fromhex(p['before'])) == {0xFF}
           for p in layer['patches']):
        return False
    return ('pages' in layer) or ('page_mask' in layer)


def plan_injection(img, layer, pages=None):
    """注入层：解析符号 → 重新汇编 → 落到新固件的空闲带。返回 (entries, blockers, info)。

    `pages` 可以覆盖这一层自带的页掩码 —— 「全开」和「默认关闭」本质上是同一段
    注入代码、只差数据块里的一个掩码值，没必要当成两层分开适配。
    """
    import symbols
    import build_widget as bw

    if not bw.keystone_ready():
        # macOS arm64 上没有 keystone 的 wheel。前两层（去进度条）只靠旧字节就行，
        # 不该因为第三层装不了就把整条链判成 blocked —— 那样这台机器上的用户
        # 什么都得不到，而实际上进度条早就能去掉。
        return [], [{'offset': p['offset'], 'kind': 'injection',
                     'reason': '这台机器没装 keystone，注入层没法重新汇编（去进度条那几层不受影响）'}
                    for p in layer['patches']], None

    sig_path = ANCHOR_DIR / 'symbols.json'
    if not sig_path.exists():
        return [], [{'offset': p['offset'], 'kind': 'injection',
                     'reason': '还没有 symbols.json（先跑 tools/symbols.py）'}
                    for p in layer['patches']], None
    sigs = symbols.load(sig_path)
    found, missing = symbols.resolve(img[:CODE_BYTES], sigs)
    if missing:
        return [], [{'offset': '<全部>', 'kind': 'injection',
                     'reason': f'这些厂商函数在新固件里找不到全片唯一的签名：{",".join(missing)}；'
                               f'代码可能被改过，不敢重新汇编'}], None
    free_at, free_len = symbols.free_band(img[:CODE_BYTES])
    need = max(len(bytes.fromhex(p['after'])) for p in layer['patches']) + 0x400
    if free_len < need:
        return [], [{'offset': hex(free_at), 'kind': 'no-room',
                     'reason': f'最大空闲带只有 {free_len} 字节，放不下 {need} 字节'}], None

    if pages is None:
        pages = layer_pages(layer)
    old = bw.relocate(syms=dict(found), free=free_at)
    try:
        total, rep = bw.patch(img, pages, strict=False)
        # ★ 必须在 restore 之前把「这一轮用到的地址」抄下来：restore 会把 CMD_TABLE
        #   等全局量还原成参考值，之后再去读就永远是旧偏移，命令项会被写到错的位置。
        cmd_table = bw.CMD_TABLE
        payload = total[:PAYLOAD_BYTES]
    finally:
        bw.restore(old)

    FREE_REF_OFF = 0x4aacc - 0x8000        # 参考镜像里空闲带的文件偏移
    entries = []
    for p in layer['patches']:
        old_off = int(p['offset'], 16)
        if old_off >= FREE_REF_OFF:
            new_off = (free_at - 0x8000) + (old_off - FREE_REF_OFF)
        else:
            new_off = cmd_table - 0x8000     # 命令表项：位置随 TBB 一起漂
        n_before = len(bytes.fromhex(p['before']))
        n_after = len(bytes.fromhex(p['after']))
        entries.append({'offset': hex(new_off), 'address': hex(new_off + 0x8000),
                        'anchor': 'reassembled', 'moved': new_off != old_off,
                        'shift': new_off - old_off,
                        'purpose': p.get('purpose'), 'note': p.get('note'),
                        # before 必须是新固件**原来**的字节（注入区应当还是 FF）
                        'before': img[new_off:new_off + n_before].hex(),
                        'after': payload[new_off:new_off + n_after].hex()})
    info = {'free_band': hex(free_at), 'free_len': free_len,
            'cmd_table': hex(cmd_table),
            'symbols': {k: hex(v) for k, v in found.items()},
            'reassembled_payload_sha256': rep['target_payload_sha256'],
            'changed_bytes': rep['changed_bytes'],
            'sectors': rep['sectors']}
    return entries, [], info


def plan_layer(img, layer, anchor_doc):
    """算出这一层在这份固件上的落点。"""
    entries, blockers, moved = [], [], []
    anchors = {a['offset']: a for a in (anchor_doc or {}).get('anchors', [])}
    for p in layer['patches']:
        old = int(p['offset'], 16)
        before = bytes.fromhex(p['before'])
        end = old + len(before)
        # ① 原位还没动过 —— 大多数情况到此为止，零成本
        if img[old:end] == before:
            entries.append({'offset': hex(old), 'address': p.get('address'),
                            'anchor': 'original', 'moved': False,
                            'purpose': p.get('purpose'), 'note': p.get('note'),
                            'before': p['before'], 'after': p['after']})
            continue
        a = anchors.get(p['offset'])
        if not a or not a.get('anchor'):
            reason = (a or {}).get('reason') or ('这一层还没有为这条补丁生成锚点'
                                                 '（tools/make_anchor.py 需要它的源镜像）')
            kind = 'injection' if '空闲带' in reason else 'unlocatable'
            blockers.append({'offset': p['offset'], 'kind': kind, 'reason': reason})
            continue
        new, why = locate(img, a)
        if new is None:
            blockers.append({'offset': p['offset'], 'kind': 'unlocatable', 'reason': why})
            continue
        got = img[new:new + len(before)]
        if got != before:
            blocker = {'offset': p['offset'], 'kind': 'mismatch',
                       'reason': f'锚点找到了位置 {new:#x}，但那儿的字节是 {got.hex()}，'
                                 f'不是这层要求的 {before.hex()} —— 这段代码可能被改过，不敢改',
                       'found_at': hex(new), 'expected': p['before'], 'found': got.hex()}
            blockers.append(blocker)
            continue
        entries.append({'offset': hex(new), 'address': p.get('address'),
                        'anchor': 'content', 'moved': True, 'from_offset': p['offset'],
                        'shift': new - old, 'fingerprint_bytes': len(bytes.fromhex(a['anchor'])),
                        'purpose': p.get('purpose'), 'note': p.get('note'),
                        'before': p['before'], 'after': p['after']})
        moved.append(new - old)
    return entries, blockers


def self_ok(img, layer):
    """这一层的补丁在这份镜像上**是否本来就打得进去**。

    只要每条补丁的旧字节不是原位还在、就是能靠锚点唯一找到，这一层就可以当起点：
    说明这份固件正好停在这一层之前的状态（前面的层要么已经打完，要么根本不需要）。
    注意只用来决定「从哪一层开始」，不做任何写入。
    """
    if layer['payload_bytes'] != len(img):
        return False
    if _already_applied(img, layer):
        return False
    doc = anchors_for(layer['profile'])
    if is_injection_layer(layer):
        # 空闲带已经被别人占住时没法重汇编（`bw.patch` 会直接抛），这一层不可能当起点
        try:
            entries, blockers, _ = plan_injection(img, layer)
        except (ValueError, ImportError):
            return False
    else:
        entries, blockers = plan_layer(img, layer, doc)
    return bool(entries) and not blockers


def chains(img):
    """把 profile 目录串成若干条链，并且允许从中间某一层起步。

    链很重要：后面几层是对前一层输出的再改写（widget-none 改的就是 widget 注入
    进去的那段代码）。逐层独立分析会得到「前面改了、后面找不到原有字节」的假结论，
    必须一层层往前推、每层拿上一层的产物当输入。

    起步点三类：
    * 链首（没有 parent 的 profile）；
    * 代码指纹正好等于某层 source 的（这份固件就是那层的输入）；
    * 某层自己能打进去、而它的父层打不进去的 —— 说明固件已经越过父层。
    """
    all_layers = {L['profile']: L for L in layers()}
    code_sha = hashlib.sha256(img[:CODE_BYTES]).hexdigest() if len(img) >= CODE_BYTES else None

    def walk(start):
        chain = [start]
        seen = {start['profile']}
        while True:
            nxt = [M for M in all_layers.values() if M.get('parent') == chain[-1]['profile']]
            if len(nxt) != 1:
                break
            child = nxt[0]
            if child['profile'] in seen:
                break           # 合并过掩码子层之后 chain[-1] 名字没变，防止原地打转
            seen.add(child['profile'])
            if is_mask_override(chain[-1], child):
                # 只改页掩码的子层：不单独走，而是并进父层（重汇编时换掩码）
                chain[-1] = dict(chain[-1], _mask_from=child)
                continue
            chain.append(child)
        return chain

    out, seen = [], set()
    starts = [L for L in all_layers.values() if L.get('parent') not in all_layers]
    if code_sha:
        starts += [L for L in all_layers.values() if L.get('source_code_sha256') == code_sha]
    for L in all_layers.values():
        parent = all_layers.get(L.get('parent'))
        if parent and self_ok(img, L) and not self_ok(img, parent):
            starts.append(L)
    for L in starts:
        chain = walk(L)
        key = tuple(x['profile'] for x in chain)
        if key in seen:
            continue
        seen.add(key)
        out.append(chain)
    if not out:
        out = [walk(L) for L in all_layers.values() if L.get('parent') not in all_layers]
    return out


def apply_entries(img, entries):
    """把一堆 {offset,before,after} 打到镜像上，返回新镜像。原地字节不符就跳过。"""
    out = bytearray(img)
    for e in entries:
        off = int(e['offset'], 16)
        before = bytes.fromhex(e['before'])
        if bytes(out[off:off + len(before)]) != before:
            continue        # 由调用方在前面已经核对过，这里只是防御
        out[off:off + len(before)] = bytes.fromhex(e['after'])
    return bytes(out)


def free_is_ff(img):
    """这份固件里那块空闲带是不是还全 0xFF（也就是「还没被注入过」）。"""
    import symbols
    at, ln = symbols.free_band(img[:CODE_BYTES])
    return set(img[at - 0x8000:at - 0x8000 + ln]) == {0xFF}, at, ln


def plan_masked_only(img, child):
    """父层已经注入过了，这一层只是要换页掩码 —— 直接改父层写进去的那几个字节。

    不重新汇编的理由：重汇编要求空闲带还是空的（`bw.patch` 只在全新镜像上成立），
    而这里父层的代码已经躺在里面了。既然只是掩码不同，就地改数据块里的那个 u32
    最稳、改动也最小（实测 366 字节，正好等于官方的 widget-none 镜像）。
    """
    doc = anchors_for(child['profile'])
    return plan_layer(img, child, doc)


def analyse(source, image_name='<firmware>'):
    img = source
    code_sha = hashlib.sha256(img[:CODE_BYTES]).hexdigest() if len(img) >= CODE_BYTES else None
    report = {'image': image_name, 'size': len(img), 'sha256': hashlib.sha256(img).hexdigest(),
              'code_sha256': code_sha, 'layers': [], 'chains': []}
    collected = []
    for chain in chains(img):
        cur = img
        steps = []
        broken = None
        for L in chain:
            if broken:
                # 上一层没套上，这一层的输入就不存在了 —— 照样记一笔，免得让人
                # 以为这一层根本没被考虑过
                rec = {'profile': L['profile'], 'label': L.get('label'),
                       'verdict': 'blocked', 'applicable': False, 'patched': 0,
                       'total': len(L['patches']), 'blockers': [], '_entries': [],
                       'stub': True,
                       'note': f'上一层 {broken} 没套上，这一层没有输入'}
                steps.append(rec)
                collected.append(rec)
                continue
            rec = _step(cur, L)
            steps.append(rec)
            collected.append(rec)
            if rec['verdict'] not in ('ready', 'empty', 'applied'):
                broken = L['profile']       # 链到这儿断了，后面的层没有输入可言
                continue
            if rec.get('patched'):
                cur = apply_entries(cur, rec['_entries'])
        boundary = next((r['verdict'] for r in steps
                         if r['verdict'] not in ('ready', 'empty', 'applied')), 'ready')
        report['chains'].append({'root': chain[0]['profile'],
                                 'steps': [r['profile'] for r in steps],
                                 'verdicts': [r['verdict'] for r in steps],
                                 'reached': boundary})
    # 同一层可能在多条链里各判一次（比如「从 reference 起步」和「直接从 widget 起步」）。
    # 对外只留最好的那个结论，否则用户会同时看到「可用」和「不可用」两个自己。
    ranks = {'ready': 5, 'applied': 4, 'partial': 3, 'empty': 2, 'size-mismatch': 1, 'blocked': 0}
    best = {}
    for rec in collected:
        if rec.get('stub'):
            continue
        if rec['profile'] not in best or ranks.get(rec['verdict'], 0) > ranks.get(best[rec['profile']]['verdict'], 0):
            best[rec['profile']] = rec
    order = [L['profile'] for L in layers()]
    by_profile = {L['profile']: L for L in layers()}
    # 「只改页掩码」的子层不单独出现在结论里：它已经并进父层了（重汇编时换了掩码），
    # 再列一遍只会让人以为还有一层要打。
    shadow = {c['profile'] for p in layers() for c in layers()
              if is_mask_override(p, c)}
    report['layers'] = []
    for p in order:
        if p in shadow:
            continue
        if p in best:
            report['layers'].append(best[p])
        else:
            L = by_profile[p]
            report['layers'].append({'profile': p, 'label': L.get('label'),
                                     'verdict': 'blocked', 'applicable': False,
                                     'patched': 0, 'total': len(L['patches']),
                                     'blockers': [{'offset': '<全部>', 'kind': 'no-anchor',
                                                   'reason': '这份固件里找不到任何能作为这一层输入的'
                                                             '位置（锚点一个都没命中）'}]})
    for r in report['layers']:
        r.pop('_entries', None)
    return report


def _already_applied(img, layer):
    """这一层是不是早就打过了（镜像已经越过它）。

    判据：每条补丁的 `after` 都在镜像里。不只看原偏移 —— 布局一漂，补丁点就跟着
    漂，所以先按锚点找位置、再看那儿是不是 `after`。注入层更简单：那段代码只要
    在片子上（不管在哪）就算打过。
    """
    if not layer['patches']:
        return False
    doc = anchors_for(layer['profile'])
    anchors = {a['offset']: a for a in (doc or {}).get('anchors', [])}
    for p in layer['patches']:
        after = bytes.fromhex(p['after'])
        before = bytes.fromhex(p['before'])
        off = int(p['offset'], 16)
        if len(after) < 32:
            if img[off:off + len(after)] == after:
                continue
            a = anchors.get(p['offset'])
            if a and a.get('anchor'):
                new, _ = locate(img, a)
                if new is not None and img[new:new + len(after)] == after:
                    continue
            return False
        else:
            if img.find(after) < 0:
                # 注入层允许「位置变了、代码没变」：按 before 全 FF 判据不算；
                # 这里只能按内容搜，搜不到就当没打
                return False
    return True


def _step(img, layer):
    merged = layer.get('_mask_from')     # 并进来的「只改页掩码」子层
    if layer['payload_bytes'] != len(img):
        return {'profile': layer['profile'], 'label': layer.get('label'),
                'verdict': 'size-mismatch', 'applicable': False,
                'reason': f'镜像 {len(img)} 字节，这一层声明 {layer["payload_bytes"]} 字节'}
    done = _already_applied(img, layer)
    if done and merged:
        # 父层的代码已经在片子上、而这一层带着「换个掩码」的子层：
        # 此刻不能重汇编（空闲带被自己占了，`bw.patch` 会在「命令表项不符」上直接抛），
        # 只能把子层那几个字节就地改过来 —— 这正是官方 widget-none 镜像的做法。
        if _already_applied(img, merged):
            return {'profile': layer['profile'], 'label': layer.get('label'),
                    'verdict': 'applied', 'applicable': False, 'patched': 0,
                    'total': len(layer['patches']), 'blockers': [], '_entries': [],
                    'note': '这一层的内容已经在固件里了，跳过'}
        cdoc = anchors_for(merged['profile'])
        entries, blockers = plan_layer(img, merged, cdoc)
        rec = {'profile': layer['profile'], 'label': layer.get('label'),
               'verdict': 'ready' if entries and not blockers else
                          ('partial' if entries else 'blocked'),
               'applicable': bool(entries) and not blockers,
               'injection': False, 'maskOnly': True,
               'anchored': len([e for e in entries if e['anchor'] == 'content']),
               'reassembled': 0,
               'patched': len(entries), 'total': len(merged['patches']),
               'blockers': blockers, '_entries': entries}
        if rec['verdict'] == 'ready':
            rec['derived'] = derive(merged, entries, img,
                                    hashlib.sha256(img[:CODE_BYTES]).hexdigest(), cdoc)
        return rec
    if done:
        return {'profile': layer['profile'], 'label': layer.get('label'),
                'verdict': 'applied', 'applicable': False, 'patched': 0,
                'total': len(layer['patches']), 'blockers': [], '_entries': [],
                'note': '这一层的内容已经在固件里了，跳过'}
    doc = anchors_for(layer['profile'])
    inj = is_injection_layer(layer)
    info = None
    if inj:
        # 空闲带还空着，可以整段重汇编；若带着换掩码的子层，就直接按子层的掩码一步到位
        pages = layer_pages(merged) if merged else None
        entries, blockers, info = plan_injection(img, layer, pages=pages)
    else:
        entries, blockers = plan_layer(img, layer, doc)
    if blockers and not entries:
        verdict = 'blocked'
    elif blockers:
        verdict = 'partial'
    elif entries:
        verdict = 'ready'
    else:
        verdict = 'empty'
    rec = {'profile': layer['profile'], 'label': layer.get('label'),
           'verdict': verdict, 'applicable': verdict in ('ready', 'partial'),
           'injection': inj,
           'anchored': len([e for e in entries if e['anchor'] == 'content']),
           'reassembled': len([e for e in entries if e['anchor'] == 'reassembled']),
           'patched': len(entries), 'total': len(layer['patches']),
           'blockers': blockers, '_entries': entries}
    if info:
        rec['injectionInfo'] = info
    if verdict in ('ready', 'partial'):
        rec['moved'] = any(e.get('moved') for e in entries)
        if rec['moved']:
            shifts = sorted({e.get('shift', 0) for e in entries if e.get('moved')})
            rec['shift'] = shifts if len(shifts) <= 4 else shifts[:3] + ['...']
    if verdict == 'ready':
        rec['derived'] = derive(layer, entries, img, hashlib.sha256(img[:CODE_BYTES]).hexdigest(), doc, info)
    return rec


def derive(layer, entries, img, code_sha, doc, info=None):
    """把算出来的新偏移组装成一个可以交给 build_fullscreen 的 profile。"""
    d = {k: v for k, v in layer.items() if k != 'patches'}
    d['patches'] = [{'address': e.get('address'), 'offset': e['offset'],
                     'purpose': e.get('purpose'), 'note': e.get('note'),
                     'before': e['before'], 'after': e['after']} for e in entries]
    d['source_sha256'] = hashlib.sha256(img).hexdigest()
    d['source_code_sha256'] = code_sha
    d['profile'] = f'auto-{code_sha[:8]}' if code_sha else 'auto'
    d['label'] = (layer.get('label') or '') + '（自动适配）'
    d['parent'] = None
    d['validation'] = 'content-anchored'
    d['derived_from'] = layer['profile']
    d['patched_payload_sha256'] = hashlib.sha256(apply_entries(img, entries)).hexdigest()
    d['target_code_sha256'] = hashlib.sha256(apply_entries(img, entries)[:CODE_BYTES]).hexdigest()
    if info:
        d['validation'] = 'reassembled'
        d['changed_bytes'] = info['changed_bytes']
        d['sectors'] = info['sectors']
        d['injection'] = {'free_band': info['free_band'], 'free_len': info['free_len'],
                          'cmd_table': info.get('cmd_table'),
                          'symbols': info['symbols'],
                          'changed_bytes': info['changed_bytes'],
                          'sectors': info['sectors']}
    else:
        # 就地改写的层：改了多少字节要自己数，否则 build_fullscreen 无法做
        # 「改动量 == 声明量」这条交叉校验。
        d['changed_bytes'] = sum(1 for a, b in zip(img, apply_entries(img, entries)) if a != b)
    if doc:
        d['anchors_profile'] = doc['profile']
    return d


# ---------------------------------------------------------------- 人话结论

def explain(report):
    lines = [f'镜像 {report["image"]}  {report["size"]} 字节  代码指纹 {str(report["code_sha256"])[:12]}']
    ready = [L for L in report['layers'] if L['verdict'] == 'ready']
    partial = [L for L in report['layers'] if L['verdict'] == 'partial']
    blocked = [L for L in report['layers'] if L['verdict'] == 'blocked']
    for L in ready:
        name = f'{L["profile"]}  {L.get("label") or ""}'
        if L.get('moved'):
            lines.append(f'  可套用  {name}  {L["patched"]}/{L["total"]} 条，其中 {L["anchored"]} 条按锚点搬移，位移 {L["shift"]}')
        else:
            lines.append(f'  可套用  {name}  {L["patched"]}/{L["total"]} 条，全部原位命中')
    for L in partial:
        kinds = sorted({b['kind'] for b in L.get('blockers', [])})
        lines.append(f'  部分可用 {name_of(L)}  卡在 {",".join(kinds)}：{L["patched"]}/{L["total"]} 条能打')
        for b in L['blockers']:
            lines.append(f'      {b["offset"]}  {b["kind"]}  {b["reason"]}')
    for L in blocked:
        kinds = sorted({b['kind'] for b in L.get('blockers', [])})
        lines.append(f'  不可用  {name_of(L)}  卡在 {",".join(kinds)}')
        for b in L['blockers'][:3]:
            lines.append(f'      {b["offset"]}  {b["kind"]}  {b["reason"]}')
        if len(L['blockers']) > 3:
            lines.append(f'      ……另外 {len(L["blockers"]) - 3} 条同类问题')
    if any(b['kind'] == 'injection' for L in report['layers'] for b in L.get('blockers', [])):
        lines.append('')
        lines.append('注入层这次没敢碰：它要往空闲带里放自己的函数，内部是')
        lines.append('  bl 0x2637c / bl 0x2625c 这样的绝对地址，换固件后厂商函数地址会一起变。')
        lines.append('  正常情况下这一层会走「符号重解析 + 重新汇编」，只在签名对不上时才放弃。')
    if not ready and not partial:
        lines.append('  结论：这份固件没有任何一层能安全套用。可以只做「原厂保留」的备份与恢复。')
    return '\n'.join(lines)


def name_of(L):
    return f'{L["profile"]}  {L.get("label") or ""}'


# ---------------------------------------------------------------- 自检

def find_clean_bases():
    """找本机上能被 profile 认领、且**每条补丁都有锚点**的源镜像。

    注入层（往空闲带里塞代码）故意排除：它的补丁点没有可锚定的上下文，放进
    「布局迁移」自检里必然永远是 partial，那是设计如此，不是缺陷。
    同一个人 profile 只留第一份镜像，避免重复跑。
    """
    cands = [ROOT / 'mcu-before-fullscreen.bin',
             ROOT.parent / 'mcu_out' / 'before_2026-11-02T17-50-51-182Z.bin',
             ROOT.parent / 'widgetlab' / 'ov6p.bin',
             Path('C:/tmp/rel15/run/orig_synth.bin')]
    cands += sorted(ROOT.parent.glob('mcu_out/*.bin'))
    found, taken = [], set()
    all_layers = layers()
    # 「只改页掩码」的子层（widget 全开 → 默认关闭）在结论里会被并进父层，不会单独出现，
    # 拿它做自检必然取不到记录。
    shadow = {c['profile'] for p in all_layers for c in all_layers if is_mask_override(p, c)}
    for p in cands:
        if not p.exists():
            continue
        img = p.read_bytes()
        if len(img) == PAYLOAD_BYTES + 16:
            img = img[:PAYLOAD_BYTES]
        if len(img) != PAYLOAD_BYTES:
            continue
        code = hashlib.sha256(img[:CODE_BYTES]).hexdigest()
        for m in all_layers:
            if m.get('source_code_sha256') != code or m['profile'] in taken:
                continue
            if m['profile'] in shadow:
                continue
            doc = anchors_for(m['profile'])
            if not doc or doc['anchored'] < doc['total']:
                continue
            taken.add(m['profile'])
            found.append((p, img, m))
            break
    return found


def shift_synthetic(img, at, extra=0x20):
    """把一段 0xFF 塞进代码区中间、末尾挤出去同样多，模拟「代码整体后移」。"""
    return img[:at] + b'\xff' * extra + img[at:PAYLOAD_BYTES - extra] + img[PAYLOAD_BYTES:]


def selftest():
    bases = find_clean_bases()
    if not bases:
        print('跳过：本机没有带锚点的源镜像')
        return 0
    passed = 0
    for path, img, layer in bases:
        print(f'-- {layer["profile"]}  源镜像 {path.name}')
        # ① 原位：应当全部命中，一条都不搬
        r = analyse(img, path.name)
        L = [x for x in r['layers'] if x['profile'] == layer['profile']][0]
        if L['verdict'] != 'ready':
            print(f'   FAIL 原位识别：{L["verdict"]} {L.get("blockers")}')
            return 0
        if L.get('moved'):
            print('   FAIL 原位不该有搬移')
            return 0
        print(f'   PASS 原位识别 {L["patched"]}/{L["total"]}')
        passed += 1

        # ② 布局迁移：在代码区中间塞 0x20 字节，锚点必须全部找回并且位移一致
        moved_img = shift_synthetic(img, 0x9000, 0x20)
        r2 = analyse(moved_img, 'shifted+0x20')
        L2 = [x for x in r2['layers'] if x['profile'] == layer['profile']][0]
        if L2['verdict'] != 'ready' or not L2.get('moved'):
            print(f'   FAIL 迁移后识别：{L2["verdict"]} moved={L2.get("moved")} 卡点={L2.get("blockers")}')
            return 0
        if L2['shift'] != [0x20]:
            print(f'   FAIL 位移应全部为 0x20，实际 {L2["shift"]}。')
            print('         注意：旧版适配器在这里会算成 0 并「假装成功」——因为它在迁移后')
            print('         的镜像上取上下文，取到的双字节本身也唯一。锚点必须先存下来。')
            return 0
        print(f'   PASS 布局迁移 +0x20：{L2["anchored"]}/{L2["total"]} 条全部按锚点搬移')
        passed += 1

        # ③ 无意义镜像：必须拒绝，不能瞎套
        junk = bytes([0x5a]) * PAYLOAD_BYTES
        r3 = analyse(junk, 'junk')
        L3 = [x for x in r3['layers'] if x['profile'] == layer['profile']][0]
        if L3['verdict'] not in ('blocked', 'partial'):
            print(f'   FAIL 垃圾镜像居然被接受：{L3["verdict"]}')
            return 0
        if L3['patched']:
            print(f'   FAIL 垃圾镜像里不该打出任何一条，实际 {L3["patched"]}')
            return 0
        print(f'   PASS 垃圾镜像拒绝（{L3["verdict"]}）')
        passed += 1

        # ④ 整条链能不能真的串起来：把派生层按 parent 顺序逐层套，每套完一层就核对
        #    「代码指纹 == 这一层自己声明的 target_code_sha256」。这一步才真正证明
        #    派生 profile 是可用的，而不只是「看起来每条都对上了」。
        layered = {x['derived']['derived_from']: x['derived']
                   for x in r['layers'] if x.get('derived')}
        if layer['profile'] in layered and r['chains']:
            root = [c for c in r['chains'] if c['root'] == layer['profile'] and c['reached'] == 'ready']
            if root:
                cur = img
                steps = root[0]['steps']
                for name in steps:
                    d = layered[name]
                    for p in d['patches']:
                        o = int(p['offset'], 16)
                        b = bytes.fromhex(p['before'])
                        a = bytes.fromhex(p['after'])
                        if cur[o:o + len(b)] != b:
                            print(f'   FAIL 串接时 {name} 的 {p["offset"]} 旧字节对不上')
                            return 0
                        cur = cur[:o] + a + cur[o + len(a):]
                    got = hashlib.sha256(cur[:CODE_BYTES]).hexdigest()
                    if got != d['target_code_sha256']:
                        print(f'   FAIL 套完 {name} 代码指纹 {got[:12]} ≠ 声明的 {d["target_code_sha256"][:12]}')
                        return 0
                print(f'   PASS 整链串接 {len(steps)} 层，每层代码指纹与声明一致')
                passed += 1
    print(f'\n共 {passed} 项通过')
    return passed


def dfu_wrap(payload, profile):
    """给载荷补上 DFU 后缀（16 字节头 + crc32 反码），跟 build_fullscreen 一模一样的算法。"""
    out = bytearray(payload)
    out.extend(bytes.fromhex(profile['dfu_suffix_prefix_hex']))
    out.extend(struct.pack('<I', (~zlib.crc32(out)) & 0xffffffff))
    return bytes(out)


def best_chain(report):
    """从报告里挑一条「走得最远」的链，返回它的层名序列。

    同长度时优先「链首更靠前」的那条 —— 从原版一路套下来的链更接近官方发布的
    形态，比从中间某层起步拼出来的结果更可信。
    """
    order = {L['profile']: i for i, L in enumerate(layers())}
    best, best_rank = [], 99
    for c in report['chains']:
        steps = c['steps']
        verdicts = c.get('verdicts') or []
        # 链在哪一层断掉，就把那层以及它后面的一起丢掉 —— 后面的层本来就依赖它
        # （widget-none 改的就是 widget 注入的代码），留着只会算错。
        # 但在断掉之前已经ready 的层是独立成立的：macOS arm64 上没有汇编器、
        # 注入层套不上时，「去进度条」那两层照样该生效，不该连它们一起放弃。
        cut = len(steps)
        for i, v in enumerate(verdicts):
            if v not in ('ready', 'empty', 'applied'):
                cut = i
                break
        keep = steps[:cut]
        if not keep:
            continue
        rank = order.get(c['root'], 99)
        if len(keep) > len(best) or (len(keep) == len(best) and rank < best_rank):
            best, best_rank = keep, rank
    return best


def build_adapted(source, name='<firmware>'):
    """把这份固件能走的最长一条链整条套下来，产出可直接刷的完整镜像。

    返回 (payload, report, chain, meta)；一条链都走不完时 payload 为 None。
    """
    rep = analyse(source, name)
    chain = best_chain(rep)
    if not chain:
        return None, rep, [], None
    by_prof = {L['profile']: L for L in rep['layers']}
    cur = source[:PAYLOAD_BYTES]
    applied = []
    for name_ in chain:
        L = by_prof[name_]
        if L['verdict'] == 'applied':
            continue            # 这一层的内容本来就在固件里了，不用再打
        d = L['derived']
        for p in d['patches']:
            o = int(p['offset'], 16)
            b = bytes.fromhex(p['before'])
            a = bytes.fromhex(p['after'])
            if cur[o:o + len(b)] != b:
                raise ValueError(f'{name_} 的 {p["offset"]} 旧字节对不上，不该走到这一步')
            cur = cur[:o] + a + cur[o + len(a):]
        applied.append(d)
    # chain 只是层名的序列，判决要回报告里查。
    by_name = {r['profile']: r for r in rep.get('layers', ())}
    dropped = [{'profile': name, 'label': by_name.get(name, {}).get('label'),
                'verdict': by_name.get(name, {}).get('verdict'),
                'blockers': by_name.get(name, {}).get('blockers') or []}
               for name in chain
               if by_name.get(name, {}).get('verdict') not in ('ready', 'empty', 'applied')]
    if not applied:
        return None, rep, chain, None
    tail_ok = bytes(cur[CODE_BYTES:]) == source[CODE_BYTES:PAYLOAD_BYTES]
    if not tail_ok:
        raise ValueError('套完之后尾部的现场数据被改动了，拒绝产出')
    return dfu_wrap(cur, applied[-1]), rep, chain, {
        'appliedProfiles': [d['derived_from'] for d in applied],
        # 上层要靠这两个键给用户看人话：只回退到 profile 名，界面就会显示
        # 「widget-dce6dce7（自动适配）」，普通人读不出这是干什么的。
        'appliedLabels': [d.get('label') or d['derived_from'] for d in applied],
        'label': applied[-1].get('label') or applied[-1]['derived_from'],
        'validation': 'content-anchored',
        'changed_bytes': sum(d['changed_bytes'] for d in applied),
        'layers': applied,
        'dropped': dropped}


def main():
    ap = argparse.ArgumentParser(description='按内容锚点适配新固件')
    ap.add_argument('--firmware')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--emit', help='写派生 profile 的目录')
    ap.add_argument('--build', help='直接产出可刷的完整镜像（含 DFU 后缀）')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if not a.firmware:
        ap.error('需要 --firmware 或 --selftest')
    src = Path(a.firmware).read_bytes()
    if len(src) == PAYLOAD_BYTES + 16:
        # 用户直接丢进来的是带 DFU 后缀的整份镜像 —— 只看前面的载荷部分
        src = src[:PAYLOAD_BYTES]
    rep = analyse(src, Path(a.firmware).name)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else explain(rep))
    if a.build:
        try:
            payload, rep2, chain, meta = build_adapted(src, Path(a.firmware).name)
        except ValueError as e:
            print(f'没法自动适配：{e}', file=sys.stderr)
            sys.exit(3)
        if not payload:
            print('这份固件已经是能套到的最新状态，没有需要打的层。', file=sys.stderr)
            sys.exit(4)
        Path(a.build).write_bytes(payload)
        print(f'\n已写出可刷镜像 {a.build}  {len(payload)} 字节  '
              f'sha256 {hashlib.sha256(payload).hexdigest()}')
        print(f'  套用了 {meta["appliedProfiles"]}，共改 {meta["changed_bytes"]} 字节')
        print('  刷之前请先备份当前固件：python desktop/app.py --backup <文件>')
    if a.emit:
        out = Path(a.emit)
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for L in rep['layers']:
            if L['verdict'] not in ('ready', 'partial'):
                continue
            d = L['derived']
            if L['verdict'] == 'partial':
                d['validation'] = 'content-anchored-partial'
            p = out / f'{d["profile"]}-{L["profile"]}.patch.json'
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
            print(f'已写出 {p}')
            n += 1
        if not n:
            print('没有可套用的层，未写出任何 profile', file=sys.stderr)
            sys.exit(2)


if __name__ == '__main__':
    main()
