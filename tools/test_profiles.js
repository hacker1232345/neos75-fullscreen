'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto'),vm=require('vm'),assert=require('assert/strict');
const real=require('./firmware_image').profiles;
assert.equal(real.length,2);
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const fixtures=real.map((m,index)=>{
 const source=Buffer.alloc(m.payload_bytes,0x61+index);
 for(const p of m.patches)Buffer.from(p.before,'hex').copy(source,parseInt(p.offset,16));
 const target=Buffer.from(source);
 for(const p of m.patches)Buffer.from(p.after,'hex').copy(target,parseInt(p.offset,16));
 return {source,target,manifest:{...m,source_code_sha256:sha(source.subarray(0,487424)),target_code_sha256:sha(target.subarray(0,487424))}};
});
const mod={exports:{}},fakeFs={existsSync:()=>true,readdirSync:()=>['issue1.patch.json'],readFileSync:p=>JSON.stringify(fixtures[p.endsWith('fullscreen.patch.json')?0:1].manifest)};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'firmware_image.js'),'utf8'),{require:n=>n==='fs'?fakeFs:require(n),module:mod,__dirname,Buffer});
const f=mod.exports;
for(const {source,target,manifest:m} of fixtures){
 assert.equal(f.firmwareInfo(source).profile,m.profile);
 assert.equal(f.firmwareKind(source),'supported-original');
 assert.deepEqual(f.buildFirmware(source).subarray(0,source.length),target);
 assert.deepEqual(f.buildFirmware(source,true).subarray(0,source.length),target);
 assert.equal(f.firmwareKind(target),'already-fullscreen');
 assert.throws(()=>f.buildFirmware(target));
 const tail=Buffer.from(source);tail[tail.length-1]^=1;
 assert.deepEqual(f.buildFirmware(tail).subarray(487424,source.length),tail.subarray(487424));
 const wrong=Buffer.from(source);wrong[256]^=1;
 assert.equal(f.firmwareKind(wrong),'unsupported-version');assert.throws(()=>f.buildFirmware(wrong));
}
console.log('PASS automatic profile selection: both original/patched builds, exact patch placement, force retains known profile, tail preservation, changed code rejection; synthetic fixtures only');
