'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const dir=__dirname,baseline=fs.readFileSync(path.join(dir,'../mcu-before-fullscreen.bin')),targetFile=path.join(dir,'mcu-fullscreen-live-v1.bin'),target=fs.readFileSync(targetFile).subarray(0,baseline.length);
const code=fs.readFileSync(path.join(dir,'mcu_flash_live.js'),'utf8');
async function scenario(eraseSize,injectFail=false,badPreflight=false){
 const flash=Buffer.from(baseline);if(badPreflight)flash[0x9000]^=1;let erases=0,writes=0,reads=0,closed=0,fault=injectFail;const erased=[];
 const D={toIdle:async()=>{},read:async(a,n)=>{reads++;return Buffer.from(flash.subarray(a-0x8000,a-0x8000+n));},erase:async(a)=>{const start=Math.floor(a/eraseSize)*eraseSize-0x8000;flash.fill(255,start,start+eraseSize);erases++;erased.push(start);},writeBlocks:async(a,blocks)=>{for(let i=0;i<blocks.length;i++){if(fault&&i===1){fault=false;throw Error('injected write failure');}blocks[i].copy(flash,a-0x8000+4096*i);writes++;}}};
 const fakeFs={...fs,existsSync:()=>false,writeFileSync:()=>{}};
 const fakeRequire=x=>x==='./mcu_dfu'?{openDfu:async()=>({}),mk:()=>D,closeDfu:()=>closed++,sleep:async()=>{},log:()=>{}}:x==='fs'?fakeFs:require(x);
 const mod={exports:{}};const proc={argv:[],env:{}};vm.runInNewContext(code,{require:fakeRequire,module:mod,__dirname:dir,process:proc,Buffer,console});
 const run=async mode=>{proc.argv=['node','mcu_flash_live.js',mode,targetFile,path.join(dir,'../mcu-before-fullscreen.bin'),'--yes'];return mod.exports.main();};
 await run('plan').catch(e=>{if(!badPreflight)throw e;});assert.equal(erases,0);
 if(badPreflight){await assert.rejects(run('flash'));assert.equal(erases,0);return;}
 if(injectFail){await assert.rejects(run('flash'),/injected/);await run('restore');assert.deepEqual(flash,baseline);return;}
 await run('flash');assert.deepEqual(flash,target);assert.equal(erases,8);assert.equal(writes,8);await run('verify');await run('restore');assert.deepEqual(flash,baseline);assert.equal(closed,4);
}
(async()=>{await scenario(4096);await scenario(8192);await scenario(4096,true);await scenario(8192,true);await scenario(4096,false,true);console.log('PASS: 4KB/8KB erase models; plan has zero erase; exact full-bank target; verify; restore; interrupted write recovery; corrupt preflight rejects before erase');})().catch(e=>{console.error(e);process.exitCode=1;});

