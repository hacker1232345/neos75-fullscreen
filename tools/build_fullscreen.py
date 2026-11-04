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

def roots(ms=None):
    """每条链的链首 layer（没有 parent，或者 parent 不在目录里）。"""
    ms=ms or manifests()
    prof={m['profile'] for m in ms}
    return [m for m in ms if not m.get('parent') or m['parent'] not in prof]

def force_layers(source,ms=None):
    """强制模式（备份代码哈希未命中任何已知构建）时，到底能套哪些层。

    旧行为是 `layers=[ms[0]]`——只套链首 reference 层，于是 clean 层（去掉上传进度环
    的那五条补丁）永远不生效，未知固件的用户刷完仍然会看到进度条。

    新行为是**先试后写**：对每条链从链首开始逐层检查，一层的所有旧字节必须原位原样
    匹配才应用，任何一条对不上就停在这一层之前。在每条链里取「套得最多」的那一串，
    再在所有链之间取最长的一条。

    注意必须从**链首**开始连续判断，不能从中间某层开始——否则会出现「clean 层没套上、
    却把挂在 clean 之后的 widget 层单独套进去」的半套状态，比不套更危险。

    这样三种真实情形都能正确处理：
      * 未知固件只是版本差异、补丁地址没动 → 每层旧字节都对得上 → 整条链照样走完；
      * 布局真的迁移了（Issue #1 那种）→ 链首就对不上 → 每条链都返回空；
      * 中间某一层对不上 → 停在它之前，只应用前面确实匹配的层。
    """
    ms=ms or manifests()
    size=len(source)
    best=[]
    for head in roots(ms):
        if head['payload_bytes']!=size:
            continue
        trial=bytearray(source)
        applied=[]
        for layer in plan_chain(head):
            if layer['payload_bytes']!=size:
                break
            ok=True
            for p in layer['patches']:
                o=int(p['offset'],16);before=bytes.fromhex(p['before'])
                if o<256 or o+len(before)>layer['immutable_payload_bytes'] or bytes(trial[o:o+len(before)])!=before:
                    ok=False
                    break
            if not ok:
                break
            for p in layer['patches']:
                o=int(p['offset'],16)
                trial[o:o+len(bytes.fromhex(p['after']))]=bytes.fromhex(p['after'])
            applied.append(layer)
        if len(applied)>len(best):
            best=applied
    return best

def force_head(source,ms=None):
    """强制模式的兜底：一层旧字节都对不上时，仍然只套链首（保持 v1.5.0 的旧行为）。"""
    ms=ms or manifests()
    same=[m for m in roots(ms) if m['payload_bytes']==len(source)]
    return (same or [m for m in ms if m['payload_bytes']==len(source)] or ms)[0]

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

def adapt(source):
    """哈希没命中时，用内容锚点现算出每一层该打在哪。

    返回 (payload, meta) 或 (None, report)。payload 是完整镜像（含 DFU 后缀）；
    None 表示一条链都走不通，此时 report 里有每一层卡在哪、为什么。
    """
    import importlib.util as _ilu
    # 兄弟模块要按「本文件所在目录」找，不能用 ROOT：测试会把 build_fullscreen.py
    # 连同临时清单一起加载，ROOT 那时指向临时目录，拼出来的 adapt_firmware.py 并不存在。
    spec=_ilu.spec_from_file_location('neos75_adapt',Path(__file__).resolve().parent/'adapt_firmware.py')
    mod=_ilu.module_from_spec(spec);spec.loader.exec_module(mod)
    payload,report,chain,meta=mod.build_adapted(source)
    got = dict(meta) if meta else {}
    # 报告一定要带出去：上层还要用它判断「去进度条那一层到底套上没有」。
    got['adaptReport'] = report
    got['adaptChain'] = chain
    return payload, got

def _adapt_reason(report):
    """把适配报告压成一句人话，直接显示给用户 —— 别让他只看到一句「不匹配」。"""
    if not report:
        return None
    blocked=[L for L in report.get('layers',())  if L['verdict']=='blocked']
    partial=[L for L in report.get('layers',())  if L['verdict']=='partial']
    if not blocked and not partial:
        return None
    kinds=sorted({b['kind'] for L in blocked+partial for b in L.get('blockers',())})
    if 'injection' in kinds and 'unresolved' in kinds:
        return ('这份固件把小组件要用到的那几个厂商函数改过了（签名对不上），'
                '没法在不碰原厂逻辑的前提下把功能塞进去。补丁未做任何写入。')
    if 'unresolved' in kinds:
        return ('这份固件的代码布局和所有已知版本都差得比较远，连作为定位线索的字节都找不到。'
                '补丁未做任何写入；如果要试，只能用「无验证」模式，并且进度条可能去不掉。')
    if 'mismatch' in kinds:
        return ('在旧字节该出现的位置找到了不一样的内容：这份固件可能已经打过补丁，'
                '或者被别的工具改过。请先把完整备份发上来。')
    return ('这份固件有几层补丁套不上（' + '、'.join(kinds) + '），为安全起见没有写入。')


def progress_removed(meta):
    """去进度条那一层（clean）到底是「已经打过」还是「这次套上了」。

    两者都算成功。只有它落在 blocked/partial、又被别的层绕过时，进度条才会留着 ——
    这种情况必须如实告诉用户，不能因为整体「适配成功」就说进度条没了。
    """
    report = meta.get('adaptReport') or {}
    applied = set(meta.get('appliedProfiles') or ())
    for L in report.get('layers', ()):
        profile = L.get('profile') or ''
        if not profile.startswith('clean-'):
            continue
        if L.get('verdict') == 'applied' or profile in applied:
            return True
        return False
    return None


def patch(source, force=False):
    source=bytes(source)
    applied,applicable,kind=identify(source)
    ms=manifests()
    if not force and not patchable(kind):
        # 已经是链末（这台机器早就刷过最新那版）：没有可再打的层，直接说清楚。
        # 不能先去试适配 —— 已打过的镜像空闲带已被占用，适配器会抛「命令表 0x3D 项
        # 与预期不符」，用户看到的就是一句莫名其妙的内部错误，而不是「已经是最新」。
        if kind=='already-fullscreen':
            raise ValueError('备份已是全屏版，不能作为原版生成补丁。')
        # 哈希对不上不等于没救：v1.6.0 起这里改成先试「按内容锚点现算偏移」。
        # 同一份固件只是布局迁移过（换批次/换编译），锚点照样能找回去，
        # 用户不该被迫去勾「无验证」然后把进度条留在屏幕上。
        payload,meta=adapt(source)
        if payload:
            return payload,{**meta,
                            'profile':meta['appliedProfiles'][-1],
                            'label':meta.get('label','自动适配'),
                            'validation':meta.get('validation','content-anchored'),
                            'target_sha256':hashlib.sha256(payload).hexdigest(),
                            'changed_bytes':meta.get('changed_bytes',0),
                            'force':False,'appliedLabels':meta.get('appliedLabels',meta['appliedProfiles']),
                            'forceLayersMatched':False,'forceBlind':False,'adapted':True,
                            'progressRemoved':progress_removed(meta)}
        why=_adapt_reason(meta.get('adaptReport') or {})
        raise ValueError(why or '备份长度或代码区域SHA256不匹配，未能自动适配。')
    # 正常路径：按哈希衔接。
    # 强制路径：先试后写（逐层核对旧字节，对得上才应用）；一层都对不上才退回「只套链首」。
    matched=True
    if applicable:
        layers=plan_chain(applicable)
    elif force:
        layers=force_layers(source,ms)
        if not layers:
            layers=[force_head(source,ms)]
            matched=False
    else:
        layers=[ms[0]]
    if not layers or layers[0]['payload_bytes']!=len(source):
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
    if bytes(out[code_bytes:len(source)])!=source[code_bytes:]:
        raise ValueError('生成目标代码或配置保留校验不匹配')
    leaf=layers[-1]
    if not force:
        # 各层改动量不能直接相加：链里存在「后一层只改前一层刚写下的那几个字节」的组合
        # （widget 把页掩码写成 0x3f，widget-none 再把它改成 0），那些位置本来就已经不同了。
        # 上界仍然是各层之和（差异不可能更多），真正确认产物对不对的是后面那条 SHA256。
        ceiling=sum(m['changed_bytes'] for m in layers)
        if (changed>ceiling
                or hashlib.sha256(bytes(out[:code_bytes])).hexdigest()!=leaf['target_code_sha256']):
            raise ValueError('生成目标代码或配置保留校验不匹配')
    out.extend(bytes.fromhex(leaf['dfu_suffix_prefix_hex']))
    out.extend(struct.pack('<I',(~zlib.crc32(out))&0xffffffff))
    result=bytes(out)
    meta={**leaf,
          'appliedProfiles':[m['profile'] for m in layers],
          'appliedLabels':[m['label'] for m in layers],
          'actual_source_sha256':hashlib.sha256(source).hexdigest(),
          'target_sha256':hashlib.sha256(result).hexdigest(),
          'changed_bytes':changed,'force':force,
          # 强制模式下这些层是「旧字节逐条核对通过」才应用的；False 表示一次都没对上、
          # 只按链首固定地址盲套（此时输出不可当已验证结果看待）。
          'forceLayersMatched':matched and bool(force),
          'forceBlind':bool(force) and not matched}
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
