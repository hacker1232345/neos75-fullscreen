'use strict';
const assert=require('node:assert/strict'),{EventEmitter}=require('node:events'),vm=require('node:vm'),fs=require('node:fs');
const t=require('./screen_wallpaper');
const fixture=JSON.parse(fs.readFileSync(require('node:path').join(__dirname,'official-methods.json'),'utf8'));
// Independent bit-at-a-time CRC via polynomial long division (reference standard vector below).
function refcrc(bytes){let c=0xffffffff;for(const v of bytes){let x=(c^v)&255;for(let k=0;k<8;k++)x=(x&1)?0xedb88320^(x>>>1):x>>>1;c=(c>>>8)^x;}return(c^0xffffffff)>>>0;}
const orig=vm.runInNewContext('({'+Object.values(fixture.methods).join(',')+'})',{_0x37e8dc:()=>{},Uint8Array,Math,console,Buffer,w:4096,B:62,y:{JPG_HEADER:[0x55,0x42],JPG_FRAME_HEAD:[6,0xaa,0xb0]},_0x3c7c1d:refcrc});
class Mock extends EventEmitter{constructor(reply){super();this.reply=reply;this.writes=[];}write(a){this.writes.push(Buffer.from(a));this.reply?.(this,a);return a.length;}}
(async()=>{
 let n=0;const ok=()=>n++;
 assert.equal(t.crc32(Buffer.from('123456789')),0xcbf43926);ok();
 for(const len of [1,61,62,63,4095,4096,4097,8192,8201,12288]){
  const b=Buffer.from(Array.from({length:len},(_,i)=>(i*17+3)&255));
  const actual=[...t.frames(b)];const expected=[];
  orig.activeData={pictureApply:{}};orig.sendPictureFrame=async(dev,p,ack)=>{expected.push({p:Buffer.from(p),ack:ack==='required'});return true;};
  assert.equal(await orig.sendPictureStream(null,b,true),true);
  assert.equal(actual.length,expected.length);
  actual.forEach((f,i)=>{assert.equal(f.report.length,65);assert.equal(f.report[0],0);assert.equal(f.report[1],0xba);assert.deepEqual(f.report.subarray(2,2+expected[i].p.length),expected[i].p);assert(f.report.subarray(2+expected[i].p.length).every(x=>x===0));assert.equal(f.ack,expected[i].ack);});
  const recovered=Buffer.concat(actual.slice(1).map(f=>f.report.subarray(3,3+f.report[2])));assert.deepEqual(recovered,b);ok();
 }
 const jpg=Buffer.from([0xff,0xd8,0xff,0xc0,0,11,8,1,0x40,1,0x40,1,1,0x11,0,0xff,0xda,0,8,1,1,0,0,63,0,12,0xff,0xd9]);
 assert.deepEqual(t.jpegInfo(jpg),{width:320,height:320});
 assert.deepEqual(t.buildStream(jpg,320,320),Buffer.from(orig.buildPictureStream([jpg],[0],320,320,true)));ok();
 for(const b of [Buffer.alloc(1),jpg.subarray(0,-1),Buffer.from(jpg).fill(0,4,6)])assert.throws(()=>t.jpegInfo(b));ok();
 const progressive=Buffer.from(jpg);progressive[3]=0xc2;assert.throws(()=>t.jpegInfo(progressive));ok();
 const response=Buffer.from('a54100051000f00700b2','hex');assert.equal(t.parseRead(response,16,4).readUInt32LE(),520192);ok();
 for(const change of [r=>r[0]=0,r=>r[1]=0,r=>r[4]=17,r=>r[3]=63,r=>r[9]^=1]){const r=Buffer.from(response);change(r);assert.throws(()=>t.parseRead(r,16,4));}ok();
 const info=Buffer.from('4255010040014001000001','hex');t.validateTarget(info,100,{width:320,height:320},Buffer.alloc(100));
 for(const fn of [()=>t.validateTarget(info,0,{width:320,height:320},jpg),()=>t.validateTarget(info,10,{width:320,height:320},jpg),()=>t.validateTarget(info,100,{width:480,height:360},jpg)])assert.throws(fn);ok();
 const good=d=>d.emit('data',Buffer.from([0xba,0,0,0,0,0,0xfe]));
 const mock=new Mock(good);const result=await t.transmit(mock,Buffer.alloc(8193),20);assert.equal(result.acks,3);assert.equal(result.displayVerified,false);ok();
 for(const reply of [d=>d.emit('data',Buffer.from([0xba,0,0,0,0,0,0])),d=>d.emit('data',Buffer.from([0xa5,0,0,0,0,0,0xfe])),()=>{}]){const m=new Mock(reply);await assert.rejects(t.transmit(m,Buffer.alloc(4097),10));assert.equal(m.writes.length,1);assert.equal(m.listenerCount('data'),0);assert.equal(m.listenerCount('error'),0);}ok();
 const failure=new Mock(()=>{throw Error('write fail');});await assert.rejects(t.transmit(failure,Buffer.alloc(1),10));assert.equal(failure.listenerCount('data'),0);ok();
 const short=new Mock();short.write=()=>1;await assert.rejects(t.transmit(short,Buffer.alloc(1),10));ok();
 const noise=new Mock(d=>{d.emit('data',Buffer.from([0xa5]));good(d);});await t.transmit(noise,Buffer.alloc(1),10);ok();
 await assert.rejects(t.main(['send','nonexistent.jpg']),/--yes/);ok();
 console.log(`PASS ${n} groups: official differential / CRC / boundaries / validation / ACK / timeout / write failure / explicit send gate`);
})().catch(e=>{console.error(e);process.exitCode=1;});
