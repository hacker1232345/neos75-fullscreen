"""gen_knob_code.py —— 把「屏幕旋钮」两处代码的机器码预先算成一张表

为什么要这张表：桌面程序打包成单个 exe 之后，目标机器上没有 Python，也没有
keystone。屏幕旋钮的左右旋转要写字机器码，但每一段只随「动作号」变化，全部
可能值就 26 个（外加两种原厂翻页写法）× 2 个入口跨度 —— 一共 56 段固定字节。
把它们在开发机上算一次、存成 `tools/knob_code.json`，运行时直接查表，就完全
不需要运行时汇编器。

本脚本是这张表的唯一来源；`tests/test_knob_cfg.py` 会重新算一遍并比对，
表一旦被手改或与生成器逻辑脱节，测试立刻失败。

    python tools/gen_knob_code.py            # 重新生成
    python tools/gen_knob_code.py --check    # 只校验（CI 用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import knob_config as K

OUT = Path(__file__).resolve().parent / 'knob_code.json'


def table():
    """{site: {action_key: "hex bytes"}}"""
    from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)

    def asm(src, dest):
        return ks.asm(src, dest)

    out = {}
    for site, (addr, span) in K.CODE_SITES.items():
        per = {}
        for key in list(K.CATALOG):
            # 翻页也走 _emit（它内部直接返回原厂字节），这样每条都能自洽还原
            act = K.CATALOG[key]
            code = K._emit(site, act, span, addr, asm)
            per[key] = code.hex()
        out[site] = per
    return out


def dump(data):
    return json.dumps({'source': 'tools/gen_knob_code.py',
                       'base': hex(K.BASE),
                       'sites': {k: {'addr': hex(v[0]), 'span': v[1]} for k, v in K.CODE_SITES.items()},
                       'codes': data},
                      ensure_ascii=False, indent=1, sort_keys=True) + '\n'


def main(argv):
    fresh = table()
    if '--check' in argv:
        if not OUT.exists():
            sys.stdout.write('CR  knob_code.json 不存在，先跑一次不带 --check 的\n')
            return 1
        have = json.loads(OUT.read_text(encoding='utf-8'))['codes']
        if have != fresh:
            sys.stdout.write('CR  表与生成器不一致，请重新生成\n')
            diffs = [k for k in fresh if have.get(k) != fresh[k]]
            sys.stdout.write('    位置：%s\n' % diffs)
            return 1
        sys.stdout.write('OK  knob_code.json 与生成器一致（%d 个位置 × %d 个动作）\n'
                         % (len(fresh), len(next(iter(fresh.values())))))
        return 0

    OUT.write_text(dump(fresh), encoding='utf-8', newline='\n')
    total = sum(len(v) for v in fresh.values())
    sys.stdout.write('OK  写出 %s（%d 个位置 × %d 个动作 = %d 段）\n'
                     % (OUT, len(fresh), len(next(iter(fresh.values()))), total))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
