"""Generate the exact verified Neos75 image from a matching user-owned backup."""
from pathlib import Path
import argparse,hashlib,json,struct,zlib
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'mcu-before-fullscreen.bin'
MANIFEST=ROOT/'firmware/fullscreen.patch.json'

def patch(source):
    m=json.loads(MANIFEST.read_text(encoding='utf-8'))
    code_bytes=m['immutable_payload_bytes']
    if len(source)!=m['payload_bytes'] or hashlib.sha256(source[:code_bytes]).hexdigest()!=m['source_code_sha256']:
        raise ValueError('原始备份长度或代码区域SHA256不匹配，拒绝生成。仅支持README注明的实测代码版本。')
    out=bytearray(source)
    for p in m['patches']:
        o=int(p['offset'],16);before=bytes.fromhex(p['before']);after=bytes.fromhex(p['after'])
        if len(before)!=len(after) or source[o:o+len(before)]!=before:
            raise ValueError('旧字节不匹配：'+p['address'])
        out[o:o+len(after)]=after
    if sum(a!=b for a,b in zip(source,out))!=m['changed_bytes'] or out[:256]!=source[:256]:
        raise ValueError('补丁范围不符合约束')
    out.extend(bytes.fromhex(m['dfu_suffix_prefix_hex']))
    out.extend(struct.pack('<I',(~zlib.crc32(out))&0xffffffff))
    result=bytes(out)
    if hashlib.sha256(result[:code_bytes]).hexdigest()!=m['target_code_sha256'] or result[code_bytes:len(source)]!=source[code_bytes:]:
        raise ValueError('生成目标代码或配置保留校验不匹配')
    m={**m,'actual_source_sha256':hashlib.sha256(source).hexdigest(),'target_sha256':hashlib.sha256(result).hexdigest()}
    return result,m

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE)
    p.add_argument('--out',type=Path,default=ROOT/'tools/mcu-fullscreen-live-v1.bin')
    a=p.parse_args();result,m=patch(a.source.read_bytes())
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_bytes(result)
    print(json.dumps({'bytes':len(result),'sha256':m['target_sha256'],'changed_bytes':m['changed_bytes']},ensure_ascii=False))
