"""Generate the exact verified Neos75 image from a matching user-owned backup."""
from pathlib import Path
import argparse,hashlib,json,struct,zlib
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'mcu-before-fullscreen.bin'
MANIFEST=ROOT/'firmware/fullscreen.patch.json'

def manifests():
    return [json.loads(p.read_text(encoding='utf-8')) for p in [MANIFEST,*sorted((ROOT/'firmware/profiles').glob('*.patch.json'))]]

def identify(source):
    for m in manifests():
        if len(source)!=m['payload_bytes']:continue
        digest=hashlib.sha256(source[:m['immutable_payload_bytes']]).hexdigest()
        if digest==m['source_code_sha256']:return m,'supported-original'
        if digest==m['target_code_sha256']:return m,'already-fullscreen'
    return None,'unsupported-version'

def firmware_info(source):
    m,kind=identify(source)
    return {'firmwareKind':kind,'profile':m['profile'] if m else None,'profileLabel':m['label'] if m else None,'validation':m['validation'] if m else None}

def patch(source, force=False):
    m,kind=identify(source)
    if m is None:
        if not force:raise ValueError('备份长度或代码区域SHA256不匹配，未找到对应固件配置。')
        m=manifests()[0]
    code_bytes=m['immutable_payload_bytes']
    if len(source)!=m['payload_bytes']:
        raise ValueError('原始备份长度不完整，拒绝生成。')
    if not force and kind!='supported-original':
        raise ValueError('备份已是全屏版，不能作为原版生成补丁。')
    out=bytearray(source)
    for p in m['patches']:
        o=int(p['offset'],16);before=bytes.fromhex(p['before']);after=bytes.fromhex(p['after'])
        if len(before)!=len(after) or o<256 or o+len(after)>code_bytes:
            raise ValueError('补丁范围不符合约束')
        if not force and source[o:o+len(before)]!=before:
            raise ValueError('旧字节不匹配：'+p['address'])
        out[o:o+len(after)]=after
    changed=sum(a!=b for a,b in zip(source,out))
    if (not force and changed!=m['changed_bytes']) or out[:256]!=source[:256]:
        raise ValueError('补丁范围不符合约束')
    out.extend(bytes.fromhex(m['dfu_suffix_prefix_hex']))
    out.extend(struct.pack('<I',(~zlib.crc32(out))&0xffffffff))
    result=bytes(out)
    if (not force and hashlib.sha256(result[:code_bytes]).hexdigest()!=m['target_code_sha256']) or result[code_bytes:len(source)]!=source[code_bytes:]:
        raise ValueError('生成目标代码或配置保留校验不匹配')
    m={**m,'actual_source_sha256':hashlib.sha256(source).hexdigest(),'target_sha256':hashlib.sha256(result).hexdigest(),'changed_bytes':changed,'force':force}
    return result,m

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE)
    p.add_argument('--out',type=Path,default=ROOT/'tools/mcu-fullscreen-live-v1.bin')
    p.add_argument('--force',action='store_true',help='无验证，直接打补丁：跳过版本哈希及补丁旧字节检查')
    a=p.parse_args()
    if a.source.resolve()==a.out.resolve():p.error('输出不能覆盖原始备份')
    result,m=patch(a.source.read_bytes(),force=a.force)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_bytes(result)
    print(json.dumps({'bytes':len(result),'sha256':m['target_sha256'],'changed_bytes':m['changed_bytes'],'force':m['force'],'profile':m['profile'],'validation':m['validation']},ensure_ascii=False))
