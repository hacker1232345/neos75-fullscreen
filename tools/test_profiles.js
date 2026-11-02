'use strict';
// 离线自测：补丁链自动选择。全部使用合成镜像，不打开 USB 设备、不读写真实固件。
const fs=require('fs'),path=require('path'),crypto=require('crypto'),vm=require('vm'),assert=require('assert/strict');
const real=require('./firmware_image').profiles;
const CODE=487424;
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const off=p=>parseInt(p.offset,16);
const bytes=(p,k)=>Buffer.from(p[k],'hex');
const applied=(m)=>m.patches.reduce((n,p)=>n+bytes(p,'before').filter((v,i)=>v!==bytes(p,'after')[i]).length,0);

const ref=real.find(m=>m.profile==='reference-66679ff2');
const clean=real.find(m=>m.profile==='clean-8465dd63');
const issue=real.find(m=>m.profile==='issue-1-74f2fa47');
assert.ok(ref&&clean&&issue,'目录里应有三层 profile');

// ① 用真实补丁记录、合成字节造出一条真实可比的两层链：
//    origin --reference--> fullscreen --clean--> enhanced
const origin=Buffer.alloc(ref.payload_bytes,0x61);
for(const L of [ref,clean])for(const p of L.patches)bytes(p,'before').copy(origin,off(p));
const applyLayer=(base,L)=>{const out=Buffer.from(base);for(const p of L.patches)bytes(p,'after').copy(out,off(p));return out;};
const fullscreen=applyLayer(origin,ref);
const enhanced=applyLayer(fullscreen,clean);
// 两层偏移不得重叠，否则「逐层」与「一次性」的等价性不成立
const seen=new Set();
for(const L of [ref,clean])for(const p of L.patches)for(let i=0;i<bytes(p,'before').length;i++){
 assert.ok(!seen.has(off(p)+i),'补丁偏移重叠 '+p.address);seen.add(off(p)+i);
}

const syn=m=>{
 const base={...m,changed_bytes:applied(m)};
 if(m===ref)return {...base,source_code_sha256:sha(origin.subarray(0,CODE)),target_code_sha256:sha(fullscreen.subarray(0,CODE))};
 if(m===clean)return {...base,source_code_sha256:sha(fullscreen.subarray(0,CODE)),target_code_sha256:sha(enhanced.subarray(0,CODE))};
 return {...base,source_code_sha256:sha(Buffer.alloc(issue.payload_bytes,0x7a).subarray(0,CODE)),target_code_sha256:sha(Buffer.alloc(issue.payload_bytes,0x7b).subarray(0,CODE))};
};

const catalogs={'reference-66679ff2':syn(ref),'clean-8465dd63':syn(clean),'issue-1-74f2fa47':syn(issue)};
const others=['clean-8465dd63','issue-1-74f2fa47'];
const mod={exports:{}},fakeFs={
 existsSync:()=>true,
 readdirSync:()=>others.map(n=>n+'.patch.json'),
 readFileSync:p=>{
  if(p.endsWith('fullscreen.patch.json'))return JSON.stringify(catalogs['reference-66679ff2']);
  const hit=others.find(n=>p.endsWith(n+'.patch.json'));
  return JSON.stringify(catalogs[hit]);
 },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'firmware_image.js'),'utf8'),{require:n=>n==='fs'?fakeFs:require(n),module:mod,__dirname,Buffer});
const f=mod.exports;

// ② 链首：原始备份 → 一次打到链末
const root=f.firmwareInfo(origin);
assert.equal(root.firmwareKind,'supported-original');
assert.equal(root.patchable,true);
assert.equal(root.profile,ref.profile);
assert.equal(root.validation,'hardware-verified');
assert.equal(root.pending.join('|'),[ref.label,clean.label].join('|'));
assert.deepEqual(f.buildFirmware(origin).subarray(0,CODE),enhanced.subarray(0,CODE));
assert.deepEqual(f.buildFirmware(origin,true).subarray(0,CODE),enhanced.subarray(0,CODE));

// ③ 链中：只补剩下的那一层
const mid=f.firmwareInfo(fullscreen);
assert.equal(mid.firmwareKind,'upgradeable');
assert.equal(mid.patchable,true);
assert.equal(mid.pending.join('|'),[clean.label].join('|'));
assert.equal(mid.upgradeProfile,clean.profile);
assert.equal(mid.upgradeValidation,'hardware-verified');
assert.deepEqual(f.buildFirmware(fullscreen).subarray(0,CODE),enhanced.subarray(0,CODE));

// ④ 链末：无可再打
assert.equal(f.firmwareKind(enhanced),'already-fullscreen');
assert.equal(f.firmwareInfo(enhanced).patchable,false);
assert.throws(()=>f.buildFirmware(enhanced));

// ⑤ 每条记录的约束与旧字节
for(const L of [ref,clean])for(const p of L.patches){
 assert.equal(bytes(p,'before').length,bytes(p,'after').length);
 assert.ok(off(p)>=256&&off(p)+bytes(p,'after').length<=CODE);
 assert.ok(bytes(p,'before').every((v,i)=>v===origin[off(p)+i]||v===fullscreen[off(p)+i]),'旧字节应与链上实际字节一致 '+p.address);
}

// ⑥ 尾部现场数据保留（链首与链中两种入口都要保留）
const tailed=Buffer.from(origin);tailed[CODE+7]^=1;
assert.deepEqual(f.buildFirmware(origin).subarray(CODE,origin.length),origin.subarray(CODE));
assert.deepEqual(f.buildFirmware(tailed).subarray(CODE,tailed.length),tailed.subarray(CODE));
assert.deepEqual(f.buildFirmware(fullscreen).subarray(CODE,fullscreen.length),fullscreen.subarray(CODE));

// ⑦ 损坏的代码区一律拒绝
const wrong=Buffer.from(origin);wrong[256]^=1;
assert.equal(f.firmwareKind(wrong),'unsupported-version');
assert.throws(()=>f.buildFirmware(wrong));
assert.throws(()=>f.buildFirmware(origin.subarray(1),true),/长度/);
assert.equal(f.firmwareKind(Buffer.alloc(1234)),'unsupported-version');
assert.equal(f.firmwareInfo(Buffer.alloc(1234)).patchable,false);

// ⑧ 逐层应用与一次性打到链末必须等价
assert.deepEqual(f.buildFirmware(fullscreen).subarray(0,CODE),f.buildFirmware(origin).subarray(0,CODE));

// ⑨ 强制模式仍受链末目标哈希约束，不能凭空接受任意内容
const junk=Buffer.alloc(ref.payload_bytes,0x5a);
assert.deepEqual(f.buildFirmware(junk,true).subarray(0,CODE),applyLayer(junk,ref).subarray(0,CODE));

console.log('PASS 补丁链自动选择：链首/链中/链末三态、逐层与一次性等价、旧字节与目标哈希、尾部现场数据保留、损坏代码拒绝、强制模式仍受目标约束；仅用合成镜像');
