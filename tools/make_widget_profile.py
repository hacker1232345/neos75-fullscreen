"""Generate a widget profile record from a clean-layer image.

组件层是把「覆盖层代码」写进代码区空闲带，并改写 0x3D 命令项，让屏幕侧接收器
可以按页掩码把四行文字直接画在屏幕上。本脚本只产出 profile 记录，不写设备。

用法一：直接挂在 clean 层之后（新链）

    python tools/make_widget_profile.py --clean ../mcu_out/before_2026-11-03T09-48-48-501Z.bin \
        --out firmware/profiles/widget.patch.json --pages none

用法二：挂在已有组件层之后（升级链，例如把「全页显示」改成「默认不显示」）

    python tools/make_widget_profile.py --clean ../mcu_out/before_2026-11-03T09-48-48-501Z.bin \
        --base ../widgetlab/ov6p.bin --parent widget-dce6dce7 \
        --out firmware/profiles/widget-none.patch.json --pages none

`--clean` 必须始终是 clean 的整片备份（代码哈希 8465dd63），因为覆盖层代码是相对
clean 布局生成的；`--base` 决定这一层「从哪个镜像继续往前推」，默认等于 `--clean`。
"""
from pathlib import Path
import argparse,hashlib,json,sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
import build_widget as w

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUT=ROOT/'firmware/profiles/widget.patch.json'


def merge_spans(before, after, gap=0x40):
    """把差异合并成尽量少的连续区间；中间允许夹杂未变化字节（整段覆盖写更安全）。"""
    diff=[i for i in range(len(before)) if before[i]!=after[i]]
    if not diff:
        return []
    spans=[[diff[0],diff[0]]]
    for i in diff[1:]:
        if i==spans[-1][1]+1:
            spans[-1][1]=i
        else:
            spans.append([i,i])
    merged=[]
    for s in spans:
        if merged and s[0]-merged[-1][1]<=gap:
            merged[-1][1]=s[1]
        else:
            merged.append(s)
    return merged


def lookup_parent(code_sha):
    """按代码哈希在已有 profile 里找父层：谁的 target 等于这一层的 source，谁就是它爹。

    找不到才退回「clean-<哈希前8位>」——那种情况说明这一层挂在一个目录里没有的镜像上，
    profile 依然可生成，只是链衔接得手工填。
    """
    folder=ROOT/'firmware/profiles'
    fallback='clean-'+code_sha[:8]
    if not folder.is_dir():
        return fallback
    hits=[]
    for path in sorted(folder.glob('*.json')):
        try:
            record=json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if record.get('target_code_sha256')==code_sha:
            hits.append(record.get('profile'))
    if len(hits)==1:
        return hits[0]
    if hits:
        # 多个候选时不猜：让调用者用 --parent 明说，避免把两条链接错。
        raise SystemExit('多个 profile 都衔接到同一份代码：%s；请用 --parent 指定' % '、'.join(map(str,hits)))
    return fallback


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--clean',type=Path,required=True,help='clean 层整片备份（491520 字节，代码哈希 8465dd63）')
    ap.add_argument('--base',type=Path,help='这一层的输入镜像，默认等于 --clean')
    ap.add_argument('--pages',default='none',help="none 表示装上但一页都不显示（默认）")
    ap.add_argument('--parent',help='父 profile 名，默认按 --base 的代码哈希在已有 profile 里查，查不到才退回 clean-xxxxxxxx')
    ap.add_argument('--label',help='默认：有页面 → 任意页覆盖组件；一页都不选 → 任意页覆盖组件（默认关闭）')
    ap.add_argument('--profile',help='profile 名，默认 widget-<目标代码哈希前8位>（不选页面时 widget-none-<哈希>）')
    ap.add_argument('--state',default='widget')
    ap.add_argument('--validation',default='offline-validated-awaiting-hardware')
    ap.add_argument('--out',type=Path,default=DEFAULT_OUT)
    a=ap.parse_args()

    clean=a.clean.read_bytes()
    text=a.pages.strip().lower()
    pages=[] if text in ('','none','off','0') else sorted({int(x) for x in text.split(',') if x.strip()})

    total,report=w.patch(clean,pages)          # 以 clean 为基准生成目标镜像
    target=total[:w.LENGTH]
    base=(a.base or a.clean).read_bytes()[:w.LENGTH]
    if len(base)!=w.LENGTH:
        raise SystemExit(f'--base 必须是 491520 字节，收到 {len(base)}')

    before_code=hashlib.sha256(base[:w.CODE_LENGTH]).hexdigest()
    after_code=hashlib.sha256(target[:w.CODE_LENGTH]).hexdigest()
    if before_code==after_code:
        raise SystemExit('这一层不产生任何代码变化，拒绝生成空 profile')

    spans=merge_spans(base,target)
    patches=[]
    for s,e in spans:
        start,end=w.BASE+s,w.BASE+e
        kind='cmd' if end<=w.CMD_TABLE else 'code'
        patches.append({
            'address':hex(start),
            'offset':hex(s),
            'before':bytes(base[s:e+1]).hex(),
            'after':bytes(target[s:e+1]).hex(),
            'purpose':'命令项改指覆盖层接收器' if kind=='cmd' else '覆盖层代码段',
            'note':('0x3D 命令跳转表项：原厂默认返回 → 覆盖层接收器' if kind=='cmd'
                    else f'覆盖层代码与数据块 {hex(start)}..{hex(end)}'),
        })

    parent=a.parent or lookup_parent(before_code)
    suffix='' if pages else 'none-'
    record={
        'profile':a.profile or ('widget-'+suffix+after_code[:8]),
        'label':a.label or ('任意页覆盖组件' if pages else '任意页覆盖组件（默认关闭）'),
        'state':a.state,
        'parent':parent,
        'source_sha256':hashlib.sha256(base).hexdigest(),
        'source_code_sha256':before_code,
        'target_code_sha256':after_code,
        'patched_payload_sha256':hashlib.sha256(target).hexdigest(),
        'payload_bytes':w.LENGTH,
        'immutable_payload_bytes':w.CODE_LENGTH,
        'preserved_tail_bytes':w.LENGTH-w.CODE_LENGTH,
        'changed_bytes':sum(x!=y for x,y in zip(base,target)),
        'sectors':sorted({hex(w.BASE+(i//4096)*4096) for i in range(w.LENGTH) if base[i]!=target[i]}),
        'dfu_suffix_prefix_hex':'ffff00104255000155464410',
        'pages':pages,
        'page_mask':report['layout']['data']['mask'],
        'patches':patches,
        'validation':a.validation,
    }
    if a.out.exists():
        try:
            old=json.loads(a.out.read_text(encoding='utf-8'))['profile']
        except Exception:
            old=None
        if old and old!=record['profile']:
            print(f'注意：{a.out.name} 原先是 {old}，将被替换为 {record["profile"]}')
    a.out.write_text(json.dumps(record,ensure_ascii=False,indent=2)+chr(10),encoding='utf-8')
    print(json.dumps({k:record[k] for k in ('profile','parent','source_code_sha256','target_code_sha256',
                                            'changed_bytes','sectors','page_mask','pages','validation')},
                     ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
