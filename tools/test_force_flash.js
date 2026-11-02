'use strict';
// In-memory unknown firmware only: no USB module or physical device is opened.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const {buildFirmware,manifest}=require('./firmware_image');
const baseline=Buffer.alloc(manifest.payload_bytes,0xa5);
for(const p of manifest.patches)Buffer.from(p.before,'hex').copy(baseline,parseInt(p.offset,16));
baseline[parseInt(manifest.patches[0].offset,16)]^=0x80;
const target=buildFirmware(baseline,true),code=fs.readFileSync(path.join(__dirname,'mcu_flash_live.js'),'utf8');
assert.throws(()=>buildFirmware(baseline));
assert.throws(()=>buildFirmware(baseline.subarray(1),true),/长度/);
async function scenario(eraseSize,{failure=false,corrupt=false,tamperedTarget=false,confirmed=true}={}){
 const flash=Buffer.from(baseline);if(corrupt)flash[0x9000]^=1;
 let erases=0,opens=0,closed=0,fault=failure;
 const D={toIdle:async()=>{},read:async(a,n)=>Buffer.from(flash.subarray(a-0x8000,a-0x8000+n)),erase:async(a)=>{erases++;const o=Math.floor(a/eraseSize)*eraseSize-0x8000;flash.fill(255,o,o+eraseSize);},writeBlocks:async(a,blocks)=>{for(let i=0;i<blocks.length;i++){if(fault&&i===1){fault=false;throw Error('injected failure');}blocks[i].copy(flash,a-0x8000+i*4096);}}};
 const bad=Buffer.from(target);if(tamperedTarget)bad[0x20000]^=1;
 const fakeFs={...fs,readFileSync:p=>p==='baseline.bin'?baseline:p==='target.bin'?bad:fs.readFileSync(p),existsSync:()=>false,writeFileSync:()=>{}};
 const req=x=>x==='./mcu_dfu'?{openDfu:async()=>{opens++;return {};},mk:()=>D,kick:async()=>{},closeDfu:()=>closed++,sleep:async()=>{},log:()=>{}}:x==='fs'?fakeFs:require(x);
 const proc={env:{NEOS75_BASELINE:'baseline.bin'},argv:['node','mcu_flash_live.js','--force']},mod={exports:{}};
 vm.runInNewContext(code,{require:req,module:mod,process:proc,__dirname,Buffer,console});
 const run=mode=>{proc.argv=['node','mcu_flash_live.js',mode,'target.bin','baseline.bin','--force',...(confirmed?['--yes']:[])];return mod.exports.main();};
 if(tamperedTarget||!confirmed){await assert.rejects(run('flash'));assert.equal(opens,0);assert.equal(erases,0);return;}
 await run('plan').catch(e=>{if(!corrupt)throw e;});assert.equal(erases,0);
 if(corrupt){await assert.rejects(run('flash'),/现场扇区/);assert.equal(erases,0);return;}
 if(failure){await assert.rejects(run('flash'),/injected failure/);await run('restore');assert.deepEqual(flash,baseline);return;}
 await run('flash');assert.deepEqual(flash,target.subarray(0,baseline.length));
 await run('verify');await run('restore');assert.deepEqual(flash,baseline);assert.equal(closed,4);
}
(async()=>{
 for(const size of [4096,8192]){await scenario(size);await scenario(size,{failure:true});await scenario(size,{corrupt:true});}
 await scenario(4096,{tamperedTarget:true});await scenario(4096,{confirmed:false});
 console.log('PASS forced unknown firmware: 4KB/8KB erase models, exact target, restore, interrupted recovery, corrupt preflight/target rejection, explicit write confirmation retained');
})().catch(e=>{console.error(e);process.exitCode=1;});
