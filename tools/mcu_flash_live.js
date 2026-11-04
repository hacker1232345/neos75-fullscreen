'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {openDfu,mk,kick,closeDfu,sleep,log}=require('./mcu_dfu');
const {buildFirmware,firmwareKind,firmwareInfo,selectManifest,chainOf,profiles}=require('./firmware_image');
const backupPath=process.env.NEOS75_BASELINE||path.join(__dirname,'..','mcu-before-fullscreen.bin');
const baseline=fs.readFileSync(backupPath);
const force=process.argv.includes('--force');
// 自动适配路径：上游（Python）已经按内容锚点算好产物并通过了自己那套校验，
// 这里核对它报出来的 sha256 与现场备份的尾部现场数据，而不是重算一遍 ——
// 重算用的是 Node 这边的固定偏移，对迁移过布局的固件必然算不出来。
const expectIndex=process.argv.indexOf('--expect');
const expectSha=expectIndex>0?process.argv[expectIndex+1]:null;
if(baseline.length!==491520)throw Error('现场备份长度不完整');
// 允许从「原版」或「已打过前几层、还可继续升级」的现场固件开工，两者都按哈希精确判定。
if(!force&&!expectSha&&!firmwareInfo(baseline).patchable)throw Error('现场备份代码区域SHA256不符，且不可继续升级');
const profile=expectSha?null:selectManifest(baseline,force);
// 补丁链：现场固件可能位于链的中间，允许的擦写范围要覆盖从这一层到链末的全部改动。
// 自动适配没有「链」的概念，允许范围取所有已知补丁点的扇区并集（适配器只会改这些位置）。
const layers=expectSha?[]:(force?[profile]:chainOf(profile));
const official=Buffer.concat([baseline,Buffer.alloc(16)]);
const BASE=0x8000,SEC=8192,LEN=official.length-16;
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function diff(a,b){let n=0;for(let i=0;i<a.length;i++)if(a[i]!==b[i])n++;return n;}
function allowedSectors(list){
 return [...new Set(list.flatMap(m=>m.patches.map(p=>BASE+Math.floor(parseInt(p.offset,16)/SEC)*SEC)))].sort((a,b)=>a-b);
}
async function main(){
 const [mode,file,backupFile]=process.argv.slice(2);if(!['backup','plan','flash','restore','verify'].includes(mode)||!file)throw Error('backup out | flash target verified-backup --yes | verify target');
 if(['flash','restore'].includes(mode)&&!process.argv.includes('--yes'))throw Error('flash需要--yes');
 const target=mode==='backup'?null:fs.readFileSync(file).subarray(0,LEN);
 if(target&&target.length!==LEN)throw Error('MCU载荷长度错误');
 const sectors=[];if(target)for(let o=0;o<LEN;o+=SEC)if(!target.subarray(o,o+SEC).equals(official.subarray(o,Math.min(LEN,o+SEC))))sectors.push(o);
  // 自动适配的产物核对放在最前面：plan / verify 这些只读模式同样要先证明「这份文件没问题」，
  // 否则预检通过、真刷时才因为文件被换掉而炸。
  const shipped=target?fs.readFileSync(file):null;
  if(expectSha&&target){
   if(sha(shipped)!==expectSha)throw Error('目标固件与适配器报告的 SHA256 不符（文件被改过？）');
   const tailStart=LEN-4096;
   if(!target.subarray(tailStart).equals(official.subarray(tailStart,LEN)))throw Error('目标固件动了尾部现场数据，拒绝写入');
   if(!target.subarray(0,256).equals(official.subarray(0,256)))throw Error('目标固件动了向量表，拒绝写入');
   // 适配器只改它自己找到的那几处；有差异的扇区必须落在已知补丁可能出现的范围内。
   const known=new Set();
   for(const m of profiles)for(const pp of m.patches)known.add(BASE+Math.floor(parseInt(pp.offset,16)/SEC)*SEC);
   for(const o of sectors)if(!known.has(BASE+o))throw Error('适配产物改到了未预期的扇区 '+hexAddr(BASE+o)+'，拒绝写入');
  }
  if(['flash','restore'].includes(mode)){
   const backup=fs.readFileSync(backupFile);if(!backup.equals(official.subarray(0,LEN)))throw Error('备份不等于本机原始MCU，禁止套用补丁');
   if(!expectSha){
    if(!shipped.equals(buildFirmware(baseline,force)))throw Error('目标固件不等于当前备份生成结果（含配置和DFU校验）');
    const allowed=allowedSectors(layers);
    if(force?sectors.some(o=>!allowed.includes(BASE+o)):sectors.map(o=>BASE+o).join(',')!==allowed.join(','))throw Error('差异范围异常');
   }
  }
  if(expectSha){
   log('自动适配模式：偏移按内容锚点现算，已核对产物 SHA256、向量表与尾部现场数据');
   log('改动扇区',sectors.map(o=>hexAddr(BASE+o)).join(',')||'（无）');
  }else{
   log('自动选择固件配置',profile.profile,profile.validation);
   log('补丁链',layers.map(m=>m.label).join(' → '),'允许擦写组',allowedSectors(layers).map(x=>hexAddr(x)).join(','));
  }
 if(force)log('无验证模式：已跳过版本哈希和补丁旧字节检查；未知构建使用原验证构建的固定偏移，不代表自动适配。保留备份、现场扇区检查与写后回验。');
 let s=await openDfu(8000);if(!s){await kick();s=await openDfu(40000);}if(!s)throw Error('屏幕DFU不可用');
 const D=mk(s);
 const read=async(addr,len)=>{const out=Buffer.alloc(len);for(let i=0;i<len;i+=64){const n=Math.min(64,len-i),b=await D.read(addr+i,n);if(b.length!==n)throw Error('短读');b.copy(out,i);}return out;};
 try{
  if(mode==='backup'){
   const fd=fs.openSync(file,'w');let mismatch=0;
   try{for(let o=0;o<LEN;o+=SEC){const n=Math.min(SEC,LEN-o),b=await read(BASE+o,n);fs.writeSync(fd,b);fs.fsyncSync(fd);mismatch+=diff(b,official.subarray(o,o+n));log(`备份 ${o+n}/${LEN}，累计官方差异 ${mismatch}`);}}finally{fs.closeSync(fd);}
   const b=fs.readFileSync(file);if(b.length!==LEN)throw Error('备份长度不完整');log('BACKUP',b.length,'SHA256',sha(b),'官方差异',mismatch);return;
  }
  // 整组快照包含边界邻居；任一组未通过预检前不擦除。
  const snapshots=[];
  for(const o of sectors){const b=await read(BASE+o,SEC);if(mode!=='restore'&&!b.equals(official.subarray(o,o+SEC))&&!b.equals(target.subarray(o,o+SEC)))throw Error('现场扇区既非原版也非目标 @'+hexAddr(BASE+o));snapshots.push({o,b});log('预检扇区',hexAddr(BASE+o),sha(b));}
  if(mode==='plan'){log('PLAN PASS，'+snapshots.length+'个8KB组包含邻居保护，未擦写');return;}
  if(mode==='verify'){for(const {o,b} of snapshots)if(!b.equals(target.subarray(o,o+SEC)))throw Error('回验不符');log('VERIFY PASS');return;}
  const writeTarget=mode==='restore'?official.subarray(0,LEN):target;
  const rb=file+'.rollback.json';if(mode==='flash'&&!fs.existsSync(rb))fs.writeFileSync(rb,JSON.stringify({base:BASE,sector:SEC,profile:profile.profile,chain:layers.map(m=>m.profile),entries:snapshots.map(({o,b})=>({address:BASE+o,sha256:sha(b),data:b.toString('base64')}))},null,2));
  for(const {o,b} of snapshots){const want=writeTarget.subarray(o,o+SEC);if(b.equals(want)){log('跳过已更新扇区',hexAddr(BASE+o));continue;}
   await D.toIdle();log('擦除',hexAddr(BASE+o));await D.erase(BASE+o);await D.toIdle();await D.erase(BASE+o+4096);await sleep(500);
   const blank=await read(BASE+o,SEC);if(!blank.every(x=>x===255))throw Error('擦除回读非全FF，停止写入');
   log('写入8192=2x4096',hexAddr(BASE+o));await D.writeBlocks(BASE+o,[want.subarray(0,4096),want.subarray(4096)]);await sleep(1500);
   const back=await read(BASE+o,SEC);if(!back.equals(want))throw Error('写后逐字节回验失败');log('扇区回验PASS',hexAddr(BASE+o));
  }
  for(const o of sectors)if(!(await read(BASE+o,SEC)).equals(writeTarget.subarray(o,o+SEC)))throw Error('最终回验失败');
  log(mode.toUpperCase()+' PASS：'+snapshots.length+'个8KB组（含邻居）逐字节回验一致');await D.toIdle();
 }finally{closeDfu(s);}
}
function hexAddr(n){return '0x'+(n>>>0).toString(16).toUpperCase().padStart(8,'0');}
module.exports={main,allowedSectors};
if(require.main===module)main().catch(e=>{console.error(e.stack);process.exitCode=1;});
