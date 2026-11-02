'use strict';
// 固件配置目录与补丁链。
//
// 【链式结构】每个 profile 描述一次「输入构建 → 输出构建」的字节级变换：
//   reference-66679ff2  原验证构建 : 原版(66679ff2)      → 全屏(cdd6334d)
//   issue-1-74f2fa47    Issue #1构建: 原版2(74f2fa47)     → 全屏2(9093f898)
//   clean-8465dd63      全屏+清爽    : 全屏(cdd6334d)      → 全屏+清爽(8465dd63)
// clean 的 source 正是 reference 的 target —— 它是挂在上一次补丁之上的第二层。
// parent 字段只用于展示顺序与「是否已到最新」判定；能否应用一律由哈希决定，
// 不做「看起来像就套用」的推断。Issue #1 链路目前没有 clean 层，因为其地址布局
// 需要重新定位，未经实机验收不得套用 reference 的偏移。
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../firmware/fullscreen.patch.json'),'utf8'));
const profileDir=path.join(__dirname,'../firmware/profiles');
const profiles=[manifest,...(fs.existsSync(profileDir)?fs.readdirSync(profileDir).filter(n=>n.endsWith('.patch.json')).sort().map(n=>JSON.parse(fs.readFileSync(path.join(profileDir,n),'utf8'))):[])];
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');

/** 补丁链深度（父 profile 数量）。reference/issue1 为 0，clean 为 1。 */
function depth(m){
 let d=0,cur=m,seen=new Set();
 while(cur&&cur.parent&&!seen.has(cur.profile)){seen.add(cur.profile);const p=profiles.find(x=>x.profile===cur.parent);if(!p)break;cur=p;d++;}
 return d;
}
/** 从根到当前的 profile 序列，用于界面展示。 */
function chainOf(m){
 const out=[];let cur=m,seen=new Set();
 while(cur&&!seen.has(cur.profile)){seen.add(cur.profile);out.unshift(cur);cur=cur.parent?profiles.find(x=>x.profile===cur.parent):null;}
 return out;
}
/**
 * 从 applicable 出发，沿 target→source 的精确哈希衔接向后收集可应用的层。
 * 只认哈希，不做「看起来像就套用」的推断：找不到下一层就停。
 */
function planChain(applicable){
 const out=[],seen=new Set();let cur=applicable;
 while(cur&&!seen.has(cur.profile)){
  seen.add(cur.profile);out.push(cur);
  const nxt=profiles.filter(m=>m.source_code_sha256===cur.target_code_sha256&&!seen.has(m.profile));
  cur=nxt.length?nxt.reduce((a,b)=>depth(b)>depth(a)?b:a):null;
 }
 return out;
}

/**
 * 判定镜像所处状态。
 *   applied    本镜像命中了某个 profile 的 target（即该补丁已在设备上）
 *   applicable 本镜像命中某个 profile 的 source（即可以继续应用该补丁）
 *   kind       supported-original 可打第一层 / upgradeable 可继续升级 /
 *              already-fullscreen 已到链末 / unsupported-version 未知
 */
function identify(image){
 if(!Buffer.isBuffer(image))image=Buffer.from(image);
 const empty={applied:null,applicable:null,kind:'unsupported-version'};
 const known=profiles.filter(m=>m.payload_bytes===image.length);
 if(!known.length)return empty;
 const code=sha(image.subarray(0,known[0].immutable_payload_bytes));
 let applied=null,applicable=null;
 for(const m of known){
  if(code===m.target_code_sha256&&(!applied||depth(m)>depth(applied)))applied=m;
  if(code===m.source_code_sha256&&(!applicable||depth(m)>depth(applicable)))applicable=m;
 }
 let kind='unsupported-version';
 if(applied&&applicable&&applied.profile!==applicable.profile)kind='upgradeable';
 else if(applied)kind='already-fullscreen';
 else if(applicable)kind='supported-original';
 else return empty;
 return {applied,applicable,kind};
}
function patchable(kind){return kind==='supported-original'||kind==='upgradeable';}
function firmwareKind(image){return identify(image).kind;}
function firmwareInfo(image){
 const {applied,applicable,kind}=identify(image);
 const cur=applied||applicable||null;
 return {
  firmwareKind:kind,
  patchable:patchable(kind),
  profile:cur?.profile||null,
  profileLabel:cur?.label||null,
  validation:cur?.validation||null,
  stateKey:cur?.stateKey||null,
  chain:(cur?chainOf(cur):[]).map(m=>m.label),
  pending:applicable?planChain(applicable).map(m=>m.label):[],
  upgradeProfile:kind==='upgradeable'?applicable.profile:null,
  upgradeLabel:kind==='upgradeable'?applicable.label:null,
  upgradeValidation:kind==='upgradeable'?applicable.validation:null,
 };
}
function selectManifest(image,force=false){
 const {applicable,kind}=identify(image);
 if(applicable&&(force||patchable(kind)))return applicable;
 if(force&&profiles.some(m=>m.payload_bytes===image.length))return manifest;
 throw Error('原始备份长度或代码区域SHA256不匹配，未找到对应固件配置');
}
function buildFirmware(source,force=false){
 if(!Buffer.isBuffer(source))source=Buffer.from(source);
 const {applicable,kind}=identify(source);
 if(!profiles.some(m=>m.payload_bytes===source.length))throw Error('原始备份长度不完整');
 let layers;
 if(applicable&&(force||patchable(kind)))layers=planChain(applicable);
 else if(force)layers=[manifest];
 else throw Error('原始备份长度或代码区域SHA256不匹配，未找到对应固件配置');
 const m=layers[layers.length-1];
 if(source.length!==m.payload_bytes)throw Error('原始备份长度不完整');
 const out=Buffer.from(source);let changed=0;
 // 逐层顺序应用；每层只认自己的旧字节，因此层与层的偏移即使相邻也不会互相污染。
 for(const layer of layers)for(const p of layer.patches){
  const o=parseInt(p.offset,16),before=Buffer.from(p.before,'hex'),after=Buffer.from(p.after,'hex');
  if(before.length!==after.length||o<256||o+after.length>layer.immutable_payload_bytes)throw Error('补丁范围不符合约束');
  if(!force&&!out.subarray(o,o+before.length).equals(before))throw Error('原字节不匹配 '+p.address);
  after.copy(out,o);
 }
 for(let i=0;i<source.length;i++)if(source[i]!==out[i])changed++;
 const expected=layers.reduce((n,l)=>n+l.changed_bytes,0);
 if((!force&&(changed!==expected||sha(out.subarray(0,m.immutable_payload_bytes))!==m.target_code_sha256))||!out.subarray(m.immutable_payload_bytes).equals(source.subarray(m.immutable_payload_bytes)))throw Error('目标代码或配置保留校验失败');
 const result=Buffer.concat([out,Buffer.from(m.dfu_suffix_prefix_hex,'hex'),Buffer.alloc(4)]);
 let crc=0xffffffff;for(const v of result.subarray(0,-4)){crc^=v;for(let k=0;k<8;k++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);} // DFU stores complement of standard CRC32.
 result.writeUInt32LE(crc>>>0,result.length-4);return result;
}
module.exports={firmwareKind,firmwareInfo,selectManifest,buildFirmware,identify,chainOf,planChain,depth,patchable,manifest,profiles};
