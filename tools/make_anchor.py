"""为每一层补丁生成「内容锚点」：一段能唯一定位补丁点、又不含被改字节本身的上下文。

为什么不能靠「补丁点那两字节」定位
--------------------------------
`adapt_firmware.py` 一开始直接在**新固件**上从旧偏移取上下文当指纹，于是布局一迁移就
取到了错位的字节 —— 它自己恰好也唯一，结果算出「位移 0」，看起来成功，实际把补丁写
到完全无关的位置。这是本工具诞生那天踩的最贵的一个坑。

正确做法是反过来：锚点必须来自**这一层的源镜像**（已知、正确的那一份），存成
`offset + anchor_hex + anchor_delta`。适配新固件时拿 anchor 去搜，搜到唯一一处，就
得到新偏移。被改的那几个字节本身不进锚点（因为有些补丁的旧字节在原片里出现几百次，
比如 `0x3654a` 的 `ff` 有 219584 处），所以锚点取的是**紧邻的非改字节**。

产物放 `firmware/anchors/<profile>.json`，不动已验证的 profile 文件。
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_DIR = ROOT / 'firmware' / 'anchors'
MAX_ANCHOR = 40


def profiles():
    out = []
    for d in (ROOT / 'firmware', ROOT / 'firmware' / 'profiles'):
        for p in sorted(d.glob('*.patch.json')):
            out.append(json.loads(p.read_text(encoding='utf-8')))
    return out


def counts(img, pat):
    n, i = 0, img.find(pat)
    while i >= 0:
        n += 1
        i = img.find(pat, i + 1)
    return n


def unique_window(img, start, end, forbidden=(), limit=MAX_ANCHOR, partner=None):
    """在 [start,end) 之外找最短的「全片唯一」窗口，返回 (delta, blob)。

    两条硬约束（都吃过亏）：

    1. **窗口绝不能盖住任何一处会被改写的字节**——不只是这条补丁自己的，还包括同一层
       别的补丁。锚点是拿源镜像截的，一旦盖住另一条补丁的旧字节，等那一层打完，这个
       锚点就再也搜不到了，`_already_applied` 会把「已经打过」误判成「还没打」，接着
       整条链被卡死。
    2. 反过来，锚点**必须在新镜像里也唯一**。所以生成时会同时拿 partner（这一层的目标
       镜像）再验一次唯一性，两边都唯一才算合格。
    """
    spans = sorted(forbidden)
    # 把与被改区间粘连的补丁带合并成一段，窗口只能落在这一整段之外
    lo_b, hi_b = start, end
    changed = True
    while changed:
        changed = False
        for a, b in spans:
            if b <= lo_b or a >= hi_b:
                continue
            if a < lo_b:
                lo_b = a
                changed = True
            if b > hi_b:
                hi_b = b
                changed = True
    for pad in range(2, limit + 1):
        for mode in ('left', 'right'):
            if mode == 'left':
                lo, hi = lo_b - pad, lo_b
            else:
                lo, hi = hi_b, hi_b + pad
            if lo < 0 or hi > len(img) or hi - lo > limit:
                continue
            blob = img[lo:hi]
            if counts(img, blob) != 1:
                continue
            if partner is not None and counts(partner, blob) != 1:
                continue        # 打完这一层就搜不到了，不合格
            return lo - start, blob
    return None


def spans_of(patches):
    """把一批补丁折成 [lo,hi) 区间。"""
    out = []
    for p in patches:
        s = int(p['offset'], 16)
        out.append((s, s + len(p['before']) // 2))
    return out


def is_injection(img, start, end, window=0x200):
    """补丁点是不是「往全 0xFF 空闲带里塞一整段代码」。

    必须看**周围**，不能只看被改的字节：有的补丁本来就是单个 `0xff`（比如 `movs r1,#0xff`
    改成 `#0x00`），它自己当然是 FF，但那明显是代码中间的一条立即数，不是填充区。
    判据：补丁两侧 window 字节里 FF 占比 > 90%，且补丁本身长度 ≥ 16。
    """
    if end - start < 16:
        return False
    lo = max(0, start - window)
    hi = min(len(img), end + window)
    around = img[lo:hi]
    return around.count(0xFF) > len(around) * 0.9


def build(profile, image, partner=None):
    """生成锚点。image 是这一层的源镜像，partner 是它的目标镜像（用来复核唯一性）。"""
    code_bytes = profile['immutable_payload_bytes']
    out = {'profile': profile['profile'],
           'label': profile.get('label'),
           'image_sha256': hashlib.sha256(image).hexdigest(),
           'source_code_sha256': profile['source_code_sha256'],
           'image_code_sha256': hashlib.sha256(image[:code_bytes]).hexdigest(),
           'anchors': []}
    ok = 0
    spans = spans_of(profile['patches'])
    for p in profile['patches']:
        start = int(p['offset'], 16)
        end = start + len(p['before']) // 2
        if is_injection(image, start, end):
            out['anchors'].append({'offset': p['offset'], 'before': p['before'],
                                   'anchor': None, 'anchor_delta': None,
                                   'reason': '落在全 0xFF 空闲带里：没有可锚定的上下文，'
                                             '这一段含绝对地址的调用目标，必须按符号重新汇编'})
            continue
        win = unique_window(image, start, end, forbidden=spans, partner=partner)
        if not win:
            out['anchors'].append({'offset': p['offset'], 'before': p['before'],
                                   'anchor': None, 'anchor_delta': None,
                                   'reason': f'前后 {MAX_ANCHOR} 字节内找不到两边都唯一的上下文'})
            continue
        delta, blob = win
        out['anchors'].append({'offset': p['offset'], 'before': p['before'],
                               'anchor': blob.hex(), 'anchor_delta': delta})
        ok += 1
    out['anchored'] = ok
    out['total'] = len(profile['patches'])
    return out


def any_image(code_sha, payload_bytes):
    """按代码哈希在本机找一份镜像（源镜像或目标镜像都用这个）。"""
    cands = [ROOT / 'mcu-before-fullscreen.bin',
             ROOT.parent / 'mcu_out' / 'before_2026-11-02T17-50-51-182Z.bin',
             ROOT.parent / 'mcu_out' / 'mcu_current_before_ringless.bin',
             ROOT.parent / 'mcu_out' / 'before_2026-11-03T09-48-48-501Z.bin',
             ROOT.parent / 'widgetlab' / 'ov6p.bin',
             ROOT.parent / 'widgetlab' / 'ov6q.bin',
             Path('C:/tmp/rel15/run/orig_synth.bin')]
    cands += sorted(ROOT.parent.glob('mcu_out/*.bin'))
    cands += sorted(ROOT.parent.glob('widgetlab/*.bin'))
    seen = set()
    for p in cands:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        img = p.read_bytes()
        if payload_bytes not in (len(img), len(img) - 16):
            continue
        body = img[:payload_bytes]
        code_bytes = len(body) - 4096      # 尾 4096 是现场数据，不进代码指纹
        if hashlib.sha256(body[:code_bytes]).hexdigest() == code_sha:
            return p, body
    return None, None


def source_image(profile):
    """在本机找这一层的源镜像（用 source_code_sha256 认领）。"""
    return any_image(profile['source_code_sha256'], profile['payload_bytes'])


def main():
    ap = argparse.ArgumentParser(description='生成内容锚点')
    ap.add_argument('--profile', help='profile 名（默认全部）')
    ap.add_argument('--image', help='这一层的源镜像（默认自动找）')
    ap.add_argument('--check', action='store_true', help='只报告，不写文件')
    a = ap.parse_args()
    ANCHOR_DIR.mkdir(parents=True, exist_ok=True)
    todo = [p for p in profiles() if a.profile in (None, p['profile'])]
    if a.profile and not todo:
        sys.exit(f'没有这个 profile: {a.profile}')
    bad = 0
    for prof in todo:
        img_path, img = (Path(a.image), Path(a.image).read_bytes()) if a.image else source_image(prof)
        if img is None:
            print(f'{prof["profile"]:<24} 跳过：本机没有它的源镜像（source {prof["source_code_sha256"][:12]}）')
            continue
        dst_path, dst = any_image(prof['target_code_sha256'], prof['payload_bytes'])
        got = build(prof, img, partner=dst)
        tag = f'{got["anchored"]}/{got["total"]}'
        print(f'{prof["profile"]:<24} {tag:<8} 源镜像 {img_path.name}'
              + (f'  目标镜像 {dst_path.name}' if dst is not None else '  （本机没有目标镜像，只验了一边的唯一性）'))
        if a.check:
            continue
        dest = ANCHOR_DIR / f'{prof["profile"]}.json'
        dest.write_text(json.dumps(got, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        if got['anchored'] < got['total']:
            bad += 1
            for x in got['anchors']:
                if not x['anchor']:
                    print(f'     {x["offset"]} 无法锚定：{x["reason"]}')
    return 0 if not bad else 0


if __name__ == '__main__':
    main()
