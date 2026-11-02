'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../firmware/fullscreen.patch.json'),'utf8'));
const profileDir=path.join(__dirname,'../firmware/profiles');
const profiles=[manifest,...(fs.existsSync(profileDir)?fs.readdirSync(profileDir).filter(n=>n.endsWith('.patch.json')).sort().map(n=>JSON.parse(fs.readFileSync(path.join(profileDir,n),'utf8'))):[])];
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function identify(image){
 for(const m of profiles){
  if(image.length!==m.payload_bytes)continue;
  const hash=sha(image.subarray(0,m.immutable_payload_bytes));
  if(hash===m.source_code_sha256)return {manifest:m,kind:'supported-original'};
  if(hash===m.target_code_sha256)return {manifest:m,kind:'already-fullscreen'};
 }
 return {manifest:null,kind:'unsupported-version'};
}
function firmwareKind(image){return identify(image).kind;}
function firmwareInfo(image){
 const {manifest:m,kind}=identify(image);
 return {firmwareKind:kind,profile:m?.profile||null,profileLabel:m?.label||null,validation:m?.validation||null};
}
function selectManifest(image,force=false){
 const found=identify(image).manifest;
 if(found)return found;
 if(force&&image.length===manifest.payload_bytes)return manifest;
 throw Error('原始备份长度或代码区域SHA256不匹配，未找到对应固件配置');
}
function buildFirmware(source,force=false){
 const m=selectManifest(source,force);
 if(source.length!==manifest.payload_bytes)throw Error('原始备份长度不完整');
 if(!force&&firmwareKind(source)!=='supported-original')throw Error('原始备份的代码区域SHA256不匹配');
 const out=Buffer.from(source);let changed=0;
 for(const p of m.patches){const o=parseInt(p.offset,16),before=Buffer.from(p.before,'hex'),after=Buffer.from(p.after,'hex');
  if(before.length!==after.length||o<256||o+after.length>m.immutable_payload_bytes)throw Error('补丁范围不符合约束');
  if(!force&&!source.subarray(o,o+before.length).equals(before))throw Error('原字节不匹配 '+p.address);
  after.copy(out,o);
 }
 for(let i=0;i<source.length;i++)if(source[i]!==out[i])changed++;
 if((!force&&(changed!==m.changed_bytes||sha(out.subarray(0,m.immutable_payload_bytes))!==m.target_code_sha256))||!out.subarray(m.immutable_payload_bytes).equals(source.subarray(m.immutable_payload_bytes)))throw Error('目标代码或配置保留校验失败');
 const result=Buffer.concat([out,Buffer.from(m.dfu_suffix_prefix_hex,'hex'),Buffer.alloc(4)]);
 let crc=0xffffffff;for(const v of result.subarray(0,-4)){crc^=v;for(let k=0;k<8;k++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);} // DFU stores complement of standard CRC32.
 result.writeUInt32LE(crc>>>0,result.length-4);return result;
}
module.exports={firmwareKind,firmwareInfo,selectManifest,buildFirmware,manifest,profiles};
