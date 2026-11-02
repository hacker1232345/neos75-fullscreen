'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../firmware/fullscreen.patch.json'),'utf8'));
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function firmwareKind(image){
 if(image.length!==manifest.payload_bytes)return 'unsupported-version';
 const hash=sha(image.subarray(0,manifest.immutable_payload_bytes));
 return hash===manifest.source_code_sha256?'supported-original':hash===manifest.target_code_sha256?'already-fullscreen':'unsupported-version';
}
function buildFirmware(source,force=false){
 if(source.length!==manifest.payload_bytes)throw Error('原始备份长度不完整');
 if(!force&&firmwareKind(source)!=='supported-original')throw Error('原始备份的代码区域SHA256不匹配');
 const out=Buffer.from(source);let changed=0;
 for(const p of manifest.patches){const o=parseInt(p.offset,16),before=Buffer.from(p.before,'hex'),after=Buffer.from(p.after,'hex');
  if(before.length!==after.length||o<256||o+after.length>manifest.immutable_payload_bytes)throw Error('补丁范围不符合约束');
  if(!force&&!source.subarray(o,o+before.length).equals(before))throw Error('原字节不匹配 '+p.address);
  after.copy(out,o);
 }
 for(let i=0;i<source.length;i++)if(source[i]!==out[i])changed++;
 if((!force&&(changed!==27||firmwareKind(out)!=='already-fullscreen'))||!out.subarray(manifest.immutable_payload_bytes).equals(source.subarray(manifest.immutable_payload_bytes)))throw Error('目标代码或配置保留校验失败');
 const result=Buffer.concat([out,Buffer.from(manifest.dfu_suffix_prefix_hex,'hex'),Buffer.alloc(4)]);
 let crc=0xffffffff;for(const v of result.subarray(0,-4)){crc^=v;for(let k=0;k<8;k++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);} // DFU stores complement of standard CRC32.
 result.writeUInt32LE(crc>>>0,result.length-4);return result;
}
module.exports={firmwareKind,buildFirmware,manifest};
