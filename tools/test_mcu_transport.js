const assert=require('assert/strict');const {mk}=require('./mcu_dfu');
const flash=Buffer.alloc(8192,255);let addr=0,state=2;const writes=[];
const response=b=>({data:new DataView(b.buffer,b.byteOffset,b.byteLength)});
const dev={clearHalt:async()=>{},controlTransferOut:async(o,b)=>{
 b=Buffer.from(b);if(o.request===6){state=2;return;}
 if(o.request===1&&o.value===0&&b[0]===0x21){addr=b.readUInt32LE(1);state=5;return;}
 if(o.request===1&&o.value===2){const at=addr-0x10000+(o.value-2)*4096;writes.push(addr);for(let i=0;i<b.length;i++){if((flash[at+i]&b[i])!==b[i])throw Error('attempted 0-to-1 flash programming');flash[at+i]&=b[i];}state=5;return;}
 throw Error('unexpected control out');
},controlTransferIn:async(o,n)=>{
 if(o.request===3){const b=Buffer.alloc(6);b[4]=state;return response(b);}
 if(o.request===2){state=9;return response(Buffer.from(flash.subarray(addr-0x10000,addr-0x10000+n)));}
 throw Error('unexpected control in');
}};
(async()=>{const d=mk({d:dev,intf:0}),a=Buffer.alloc(4096,0xa5),b=Buffer.alloc(4096,0x5a);await d.writeBlocks(0x10000,[a,b]);assert.deepEqual(writes,[0x10000,0x11000]);assert.deepEqual(flash,Buffer.concat([a,b]));assert.deepEqual(await d.readSpan(0x10000,8192),flash);console.log('PASS actual mk transport: SET_ADDRESS per block, standard block-2 addressing, no overlap, exact 64-byte readback');})().catch(e=>{console.error(e);process.exitCode=1;});
