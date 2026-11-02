'use strict';
// Official protocol: BokZEQMZ.deob.js / _bs_flat.js, class he.
const fs = require('node:fs');
function crc32(b) { let c=0xffffffff; for(const v of b){c^=v;for(let k=0;k<8;k++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0; }
function jpegInfo(b) {
  if(b.length<4 || b.readUInt16BE(0)!==0xffd8) throw Error('输入必须是 JPEG');
  let p=2, size=null, scan=false;
  while(p<b.length){
    if(b[p++]!==255) throw Error('JPEG 标记损坏');
    while(b[p]===255)p++;
    const m=b[p++]; if(m===0xd9)break;
    if(p+2>b.length)throw Error('JPEG 标记截断');
    const n=b.readUInt16BE(p);if(n<2||p+n>b.length)throw Error('JPEG 段截断');
    if([0xc0,0xc1,0xc2].includes(m)){
      if(n<8)throw Error('JPEG SOF 无效');
      size={width:b.readUInt16BE(p+5),height:b.readUInt16BE(p+3)};
      if(b[p+2]!==8 || ![1,3].includes(b[p+7]) || m!==0xc0)throw Error('仅支持 8 位 baseline 灰度/RGB JPEG，请重新导出');
    }
    if(m===0xda){scan=true;break;}p+=n;
  }
  if(!size||!size.width||!size.height||!scan||b[b.length-2]!==255||b[b.length-1]!==0xd9)throw Error('JPEG 缺少尺寸、扫描段或文件尾');
  return size;
}
function buildStream(jpeg,width,height){
  if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||width>65535||height>65535)throw Error('无效尺寸');
  const h=Buffer.alloc(13);h.write('UB',0,'ascii');h.writeUInt16BE(width,2);h.writeUInt16BE(height,4);h.writeUInt16BE(1,6);h[8]=0;h.writeUInt32BE(jpeg.length,9);return Buffer.concat([h,jpeg]);
}
function report(payload){if(payload.length>63)throw Error('报告负载超长');const b=Buffer.alloc(65);b[0]=0;b[1]=0xba;Buffer.from(payload).copy(b,2);return b;}
function* frames(stream){
  const start=Buffer.alloc(7);start.set([6,0xaa,0xb0]);start.writeUInt32BE(stream.length,3);yield {report:report(start),ack:true,kind:'header'};
  let p=0;
  for(;p+4096<=stream.length;){const end=p+4096,crc=Buffer.alloc(4);crc.writeUInt32LE(crc32(stream.subarray(p,end)));
    while(p<end){const n=Math.min(62,end-p),last=p+n===end;const data=Buffer.concat([Buffer.from([n]),stream.subarray(p,p+n),...(last?[crc]:[])]);yield {report:report(data),ack:last,kind:last?'block-end':'data'};p+=n;}
  }
  while(p<stream.length){const n=Math.min(62,stream.length-p);yield {report:report(Buffer.concat([Buffer.from([n]),stream.subarray(p,p+n)])),ack:false,kind:'tail'};p+=n;}
}
function readPacket(selector){const body=[0x41,0,1,selector];return Buffer.from([0,0xa5,...body,(body.reduce((a,b)=>a+b,0)^255)&255,...Array(58).fill(0)]);}
function parseRead(r,selector,min){
  if(!r||r.length<6||r[0]!==0xa5||r[1]!==0x41||r[2]!==0||r[4]!==selector)throw Error('读应答头不匹配');
  const n=r[3];if(n<min+1||r.length<5+n)throw Error('读应答长度不足');
  const ck=(r.subarray(1,4+n).reduce((a,b)=>a+b,0)^255)&255;
  if(r[4+n]!==ck)throw Error('读应答校验错误');return r.subarray(5,4+n);
}
// No retry: the protocol has no sequence id. Only a single active uploader is safe.
function exchange(dev,bytes,predicate,timeout=1500){
  return new Promise((resolve,reject)=>{
    let timer;
    const clean=()=>{clearTimeout(timer);dev.removeListener('data',data);dev.removeListener('error',error);};
    const error=e=>{clean();reject(e);};
    const data=r=>{r=Buffer.from(r);if(predicate(r)){clean();resolve(r);}};
    dev.on('data',data);dev.on('error',error);timer=setTimeout(()=>error(Error('等待应答超时，已中止，未重发')),timeout);
    try{const n=dev.write([...bytes]);if(n!==bytes.length)throw Error('HID 写入长度不完整');}catch(e){error(e);}
  });
}
async function transmit(dev,stream,timeout=1500){
  let reports=0,acks=0;for(const f of frames(stream)){
    if(f.ack){const r=await exchange(dev,f.report,r=>r.length>=7&&r[0]===0xba,timeout);if(r[6]!==0xfe)throw Error('设备拒绝图片块');acks++;}
    else {if(dev.write([...f.report])!==f.report.length)throw Error('HID 写入长度不完整');}
    reports++;
  }
  return {reports,acks,displayVerified:false,tailAck:stream.length%4096===0?'完整块已应答':'原厂协议尾段无应答，尾段接收未确认'};
}
function validateTarget(info,space,image,stream){
  if(info.length<11||info.readUInt16LE(0)!==0x5542||info.readUInt16LE(2)!==1)throw Error('设备身份应答不符');
  if(info[10]!==1)throw Error('设备未声明支持 JPEG');
  if(info.readUInt16LE(4)!==image.width||info.readUInt16LE(6)!==image.height)throw Error('图片尺寸与设备自报尺寸不符；不自动缩放');
  if(!Number.isInteger(space)||space<=0||stream.length>space)throw Error('设备空间不足或未知');
}
function hidModule(){try{return require('node-hid');}catch{const root=process.env.NEOS75_NODE_MODULES;if(root)return require(require('node:path').join(root,'node-hid'));throw Error('请安装 node-hid 或设置 NEOS75_NODE_MODULES 为已有 node_modules 绝对目录');}}
async function main(args){
  const [mode,file,...flags]=args;
  if(!mode||mode==='--help'){console.log('node screen_wallpaper.js inspect|plan <图片.jpg>\nnode screen_wallpaper.js status\nnode screen_wallpaper.js send <图片.jpg> --yes\n默认离线；send 可能持久保存壁纸，请勿高频推送。无线、显示效果及持久性待验证。');return;}
  if(!['inspect','plan','status','send'].includes(mode))throw Error('未知命令');
  if(mode==='status'&&(file||flags.length))throw Error('status 不接受参数');
  if(mode!=='status'&&(!file||flags.some(x=>x!=='--yes')))throw Error('参数错误');
  if(mode==='send'&&!flags.includes('--yes'))throw Error('发送需要 --yes；请先 plan');
  let image,stream;
  if(file){const jpeg=fs.readFileSync(file);image=jpegInfo(jpeg);stream=buildStream(jpeg,image.width,image.height);}
  if(['inspect','plan'].includes(mode)){
    const list=[...frames(stream)];console.log(JSON.stringify({...image,jpegBytes:stream.length-13,streamBytes:stream.length,reports:list.length,requiredAcks:list.filter(x=>x.ack).length,fullBlocks:Math.floor(stream.length/4096),tailBytes:stream.length%4096,headerReport:list[0].report.toString('hex'),offline:true},null,2));return;
  }
  const HID=hidModule();const found=HID.devices().filter(d=>d.vendorId===0x5542&&d.productId===1&&d.usagePage===0xff60&&d.usage===0x61);
  if(found.length!==1)throw Error('需且仅需一个 5542:0001 FF60/61 屏幕接口');
  const dev=new HID.HID(found[0].path);let fatal=null;const onError=e=>{fatal=e;};dev.on('error',onError);
  try{
    const read=async(s,n)=>parseRead(await exchange(dev,readPacket(s),r=>r[0]===0xa5&&r[1]===0x41&&r[4]===s),s,n);
    const info=await read(0x14,11),space=(await read(0x10,4)).readUInt32LE();
    if(fatal)throw fatal;
    if(mode==='status'){console.log(JSON.stringify({vendorId:info.readUInt16LE(0),productId:info.readUInt16LE(2),width:info.readUInt16LE(4),height:info.readUInt16LE(6),supportJpg:info[10]===1,imageSpace:space,readOnly:true},null,2));return;}
    validateTarget(info,space,image,stream);
    console.log(JSON.stringify(await transmit(dev,stream),null,2));if(fatal)throw fatal;
  }finally{dev.removeListener('error',onError);dev.close();}
}
module.exports={crc32,jpegInfo,buildStream,report,frames,readPacket,parseRead,exchange,transmit,validateTarget,main};
if(require.main===module)main(process.argv.slice(2)).catch(e=>{console.error(e.message);process.exitCode=1;});
