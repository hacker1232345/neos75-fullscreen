'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {openDfu,mk,kick,closeDfu,sleep,log}=require('./mcu_dfu');
const backupPath=path.join(__dirname,'..','mcu-before-fullscreen.bin');
const baseline=fs.readFileSync(backupPath);
if(crypto.createHash('sha256').update(baseline).digest('hex')!=='c69c38b02841c533db44e8b058a6b86d4a0e816d2bb6ce95cd2b1d5d6927016d')throw Error('现场备份SHA256不符');
const official=Buffer.concat([baseline,Buffer.alloc(16)]);
const BASE=0x8000,SEC=8192,LEN=official.length-16;
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function diff(a,b){let n=0;for(let i=0;i<a.length;i++)if(a[i]!==b[i])n++;return n;}
async function main(){
 const [mode,file,backupFile]=process.argv.slice(2);if(!['backup','plan','flash','restore','verify'].includes(mode)||!file)throw Error('backup out | flash target verified-backup --yes | verify target');
 if(['flash','restore'].includes(mode)&&!process.argv.includes('--yes'))throw Error('flash需要--yes');
 const target=mode==='backup'?null:fs.readFileSync(file).subarray(0,LEN);
 if(target&&target.length!==LEN)throw Error('MCU载荷长度错误');
 const sectors=[];if(target)for(let o=0;o<LEN;o+=SEC)if(!target.subarray(o,o+SEC).equals(official.subarray(o,Math.min(LEN,o+SEC))))sectors.push(o);
 if(['flash','restore'].includes(mode)){
  const backup=fs.readFileSync(backupFile);if(!backup.equals(official.subarray(0,LEN)))throw Error('备份不等于本机原始MCU，禁止套用补丁');
  if(sha(fs.readFileSync(file))!=='3c922e809dcfac1c35ad8f1f8caebcfc999bf6475a3e6fa22fd18c5d85a4f6e5')throw Error('目标固件SHA256不匹配');
  if(sectors.map(o=>BASE+o).join(',')!==[0x10000,0x32000,0x36000,0x42000].join(','))throw Error('差异范围异常');
 }
 let s=await openDfu(8000);if(!s){await kick();s=await openDfu(40000);}if(!s)throw Error('屏幕DFU不可用');
 const D=mk(s);
 const read=async(addr,len)=>{const out=Buffer.alloc(len);for(let i=0;i<len;i+=64){const n=Math.min(64,len-i),b=await D.read(addr+i,n);if(b.length!==n)throw Error('短读');b.copy(out,i);}return out;};
 try{
  if(mode==='backup'){
   const fd=fs.openSync(file,'w');let mismatch=0;
   try{for(let o=0;o<LEN;o+=SEC){const n=Math.min(SEC,LEN-o),b=await read(BASE+o,n);fs.writeSync(fd,b);fs.fsyncSync(fd);mismatch+=diff(b,official.subarray(o,o+n));log(`备份 ${o+n}/${LEN}，累计官方差异 ${mismatch}`);}}finally{fs.closeSync(fd);}
   const b=fs.readFileSync(file);if(b.length!==LEN)throw Error('备份长度不完整');log('BACKUP',b.length,'SHA256',sha(b),'官方差异',mismatch);return;
  }
  // Full-sector snapshots include boundary neighbors. No erase before every affected sector passed preflight.
  const snapshots=[];
  for(const o of sectors){const b=await read(BASE+o,SEC);if(mode!=='restore'&&!b.equals(official.subarray(o,o+SEC))&&!b.equals(target.subarray(o,o+SEC)))throw Error('现场扇区既非原版也非目标 @'+(BASE+o).toString(16));snapshots.push({o,b});log('预检扇区', (BASE+o).toString(16),sha(b));}
  if(mode==='plan'){log('PLAN PASS，4个8KB组包含邻居保护，未擦写');return;}
  if(mode==='verify'){for(const {o,b} of snapshots)if(!b.equals(target.subarray(o,o+SEC)))throw Error('回验不符');log('VERIFY PASS');return;}
  const writeTarget=mode==='restore'?official.subarray(0,LEN):target;
  const rb=file+'.rollback.json';if(mode==='flash'&&!fs.existsSync(rb))fs.writeFileSync(rb,JSON.stringify({base:BASE,sector:SEC,entries:snapshots.map(({o,b})=>({address:BASE+o,sha256:sha(b),data:b.toString('base64')}))},null,2));
  for(const {o,b} of snapshots){const want=writeTarget.subarray(o,o+SEC);if(b.equals(want)){log('跳过已更新扇区',o);continue;}
   await D.toIdle();log('擦除', (BASE+o).toString(16));await D.erase(BASE+o);await D.toIdle();await D.erase(BASE+o+4096);await sleep(500);
   const blank=await read(BASE+o,SEC);if(!blank.every(x=>x===255))throw Error('擦除回读非全FF，停止写入');
   log('写入8192=2x4096', (BASE+o).toString(16));await D.writeBlocks(BASE+o,[want.subarray(0,4096),want.subarray(4096)]);await sleep(1500);
   const back=await read(BASE+o,SEC);if(!back.equals(want))throw Error('写后逐字节回验失败');log('扇区回验PASS',(BASE+o).toString(16));
  }
  for(const o of sectors)if(!(await read(BASE+o,SEC)).equals(writeTarget.subarray(o,o+SEC)))throw Error('最终回验失败');
  log(mode.toUpperCase()+' PASS：4个8KB组（含邻居）逐字节回验一致');await D.toIdle();
 }finally{closeDfu(s);}
}
module.exports={main};
if(require.main===module)main().catch(e=>{console.error(e.stack);process.exitCode=1;});

