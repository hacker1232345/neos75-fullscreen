"""Generate the exact verified Neos75 screen image from a matching user-owned backup.

支持补丁链：一个原始备份可以连续应用多层字节级补丁（原版 → 全屏 → 全屏+清爽）。
链式衔接由各 profile 的 source_code_sha256 / target_code_sha256 精确决定，
每层只改少量字节且互不重叠；最终以链末 profile 的 target_code_sha256 收敛校验。
"""
from pathlib import Path
import argparse,hashlib,json,struct,zlib
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'mcu-before-fullscreen.bin'
MANIFEST=ROOT/'firmware/fullscreen.patch.json'

def manifests():
    return [json.loads(p.read_text(encoding='utf-8')) for p in [MANIFEST,*sorted((ROOT/'firmware/profiles').glob('*.patch.json'))]]

def chain_depth(m, ms=None):
    ms=ms or manifests()
    d,cur,seen=0,m,{m['profile']}
    while cur.get('parent'):
        p=[x for x in ms if x['profile']==cur['parent']]
        if not p or p[0]['profile'] in seen:break
        cur=p[0];seen.add(cur['profile']);d+=1
    return d

def chain_of(m, ms=None):
    """根 → 当前的 profile 序列，供界面展示。"""
    ms=ms or manifests()
    out,cur,seen=[],m,set()
    while cur and cur['profile'] not in seen:
        seen.add(cur['profile']);out.insert(0,cur)
        p=[x for x in ms if x['profile']==cur.get('parent')] if cur.get('parent') else []
        cur=p[0] if p else None
    return out

def identify(source):
    """返回 (applied, applicable, kind)。

    applied    本镜像命中了某个 profile 的 target（该层已在设备上）
    applicable 本镜像命中了某个 profile 的 source（可以从这一层继续打）
    kind       supported-original 可打第一层 / upgradeable 可继续升级 /
               already-fullscreen 已到链末 / unsupported-version 未知
    """
    ms=manifests()
    if not ms:return None,None,'unsupported-version'
    size=len(source)
    cand=[m for m in ms if m['payload_bytes']==size]
    if not cand:return None,None,'unsupported-version'
    code=hashlib.sha256(source[:cand[0]['immutable_payload_bytes']]).hexdigest()
    pick=lambda xs:max(xs,key=lambda m:chain_depth(m,ms)) if xs else None
    applied=pick([m for m in cand if m['target_code_sha256']==code])
    applicable=pick([m for m in cand if m['source_code_sha256']==code])
    if applied and applicable and applied['profile']!=applicable['profile']:kind='upgradeable'
    elif applied:kind='already-fullscreen'
    elif applicable:kind='supported-original'
    else:return None,None,'unsupported-version'
    return applied,applicable,kind

def plan_chain(applicable):
    """从 applicable 出发沿 target→source 精确衔接向后收集可应用的层。

    只认哈希衔接，不做「看起来像就套用」的推断：找不到下一层就停。
    """
    ms=manifests()
    out,seen,cur=[],set(),applicable
    while cur and cur['profile'] not in seen:
        seen.add(cur['profile']);out.append(cur)
        nxt=[m for m in ms if m['source_code_sha256']==cur['target_code_sha256'] and m['profile'] not in seen]
        cur=max(nxt,key=lambda m:chain_depth(m,ms)) if nxt else None
    return out

def patchable(kind):
    return kind in ('supported-original','upgradeable')

def firmware_info(source):
    source=bytes(source)
    applied,applicable,kind=identify(source)
    cur=applied or applicable
    return {
        'firmwareKind':kind,
        'patchable':patchable(kind),
        'profile':cur['profile'] if cur else None,
        'profileLabel':cur['label'] if cur else None,
        'validation':cur['validation'] if cur else None,
        'stateKey':cur.get('stateKey') if cur else None,
        'chain':[m['label'] for m in chain_of(cur)] if cur else [],
        'pending':[m['label'] for m in plan_chain(applicable)] if applicable else [],
        'upgradeProfile':applicable['profile'] if kind=='upgradeable' else None,
        'upgradeLabel':applicable['label'] if kind=='upgradeable' else None,
        'upgradeValidation':applicable['validation'] if kind=='upgradeable' else None,
    }

def patch(source, force=False):
    source=bytes(source)
    applied,applicable,kind=identify(source)
    ms=manifests()
    if not force and not patchable(kind):
        raise ValueError('备份长度或代码区域SHA256不匹配，未找到对应固件配置。' if kind=='unsupported-version'
                         else '备份已是全屏版，不能作为原版生成补丁。')
    layers=plan_chain(applicable) if applicable else [ms[0]]
    if layers[0]['payload_bytes']!=len(source):
        raise ValueError('原始备份长度不完整，拒绝生成。')
    code_bytes=layers[0]['immutable_payload_bytes']
    out=bytearray(source)
    for m in layers:
        if m['payload_bytes']!=len(source):
            raise ValueError('原始备份长度不完整，拒绝生成。')
        for p in m['patches']:
            o=int(p['offset'],16);before=bytes.fromhex(p['before']);after=bytes.fromhex(p['after'])
            if len(before)!=len(after) or o<256 or o+len(after)>m['immutable_payload_bytes']:
                raise ValueError('补丁范围不符合约束')
            if not force and out[o:o+len(before)]!=before:
                raise ValueError('旧字节不匹配：'+p['address'])
            out[o:o+len(after)]=after
    changed=sum(a!=b for a,b in zip(source,out))
    if out[:256]!=source[:256]:
        raise ValueError('补丁范围不符合约束')
    leaf=layers[-1]
    if not force:
        expected=sum(m['changed_bytes'] for m in layers)
        if (changed!=expected
                or hashlib.sha256(bytes(out[:code_bytes])).hexdigest()!=leaf['target_code_sha256']
                or bytes(out[code_bytes:len(source)])!=source[code_bytes:]):
            raise ValueError('生成目标代码或配置保留校验不匹配')
    out.extend(bytes.fromhex(leaf['dfu_suffix_prefix_hex']))
    out.extend(struct.pack('<I',(~zlib.crc32(out))&0xffffffff))
    result=bytes(out)
    meta={**leaf,
          'appliedProfiles':[m['profile'] for m in layers],
          'appliedLabels':[m['label'] for m in layers],
          'actual_source_sha256':hashlib.sha256(source).hexdigest(),
          'target_sha256':hashlib.sha256(result).hexdigest(),
          'changed_bytes':changed,'force':force}
    return result,meta

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE)
    p.add_argument('--out',type=Path,default=ROOT/'tools/mcu-fullscreen-live-v1.bin')
    p.add_argument('--force',action='store_true',help='无验证，直接打补丁：跳过版本哈希及补丁旧字节检查')
    p.add_argument('--info',action='store_true',help='只识别备份并列出补丁链，不生成')
    a=p.parse_args()
    if a.info:
        print(json.dumps(firmware_info(a.source.read_bytes()),ensure_ascii=False,indent=2))
    else:
        if a.source.resolve()==a.out.resolve():p.error('输出不能覆盖原始备份')
        result,m=patch(a.source.read_bytes(),force=a.force)
        a.out.parent.mkdir(parents=True,exist_ok=True)
        a.out.write_bytes(result)
        print(json.dumps({'bytes':len(result),'sha256':m['target_sha256'],'changed_bytes':m['changed_bytes'],'force':m['force'],'profile':m['profile'],'label':m['label'],'appliedProfiles':m['appliedProfiles'],'appliedLabels':m['appliedLabels'],'validation':m['validation']},ensure_ascii=False))
