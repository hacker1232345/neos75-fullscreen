'use strict';
// Standalone backup: no existing backup, ERASE, or flash-program command required.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {openDfu,mk,kick,closeDfu,sleep,log}=require('./mcu_dfu');
const {firmwareKind}=require('./firmware_image');
const BASE=0x8000,LENGTH=491520;
async function backupToFile(D,file,onProgress=()=>{}){
 if(fs.existsSync(file)||fs.existsSync(file+'.partial')||fs.existsSync(file+'.json'))throw Error('备份或临时文件已存在；请选择新文件名');
 fs.mkdirSync(path.dirname(path.resolve(file)),{recursive:true});
 const fd=fs.openSync(file+'.partial','wx'),hash=crypto.createHash('sha256');
 try{
  for(let o=0;o<LENGTH;o+=64){
   const a=await D.read(BASE+o,64),b=await D.read(BASE+o,64);
   if(a.length!==64||b.length!==64)throw Error('备份短读 @'+(BASE+o).toString(16));
   if(!a.equals(b))throw Error('两次读取不一致 @'+(BASE+o).toString(16));
   if(fs.writeSync(fd,a)!==64)throw Error('备份文件写入不完整');
   hash.update(a);
   if((o+64)%8192===0){fs.fsyncSync(fd);onProgress(o+64,LENGTH);}
  }
  fs.fsyncSync(fd);
 }finally{fs.closeSync(fd);}
 const digest=hash.digest('hex'),saved=fs.readFileSync(file+'.partial');
 if(saved.length!==LENGTH||crypto.createHash('sha256').update(saved).digest('hex')!==digest)throw Error('落盘校验失败，保留.partial');
 if(fs.existsSync(file))throw Error('输出文件已出现，停止覆盖');
 fs.renameSync(file+'.partial',file);
 const result={file:path.resolve(file),bytes:LENGTH,sha256:digest,complete:true,readsPerChunk:2,flashWritten:false};
 fs.writeFileSync(file+'.json',JSON.stringify(result,null,2),{flag:'wx'});
 return result;
}
async function leave(D){
 await D.toIdle();const state=await D.status();
 if(!state||state.status!==0||state.state!==2)throw Error('DFU未就绪，不能确认退出');
 await D.setAddr(BASE);await D.cout(1,new Uint8Array(0),0);const x=await D.settle();
 if(!x||x.status!==0||![2,7,8].includes(x.state))throw Error('退出DFU未确认');
}
async function main(args){
 const [file,...flags]=args;
 if(!file||flags.length!==1||flags[0]!=='--yes')throw Error('backup <新备份.bin> --yes；会进入DFU，读取后尝试回应用，不擦写Flash');
 if(fs.existsSync(file)||fs.existsSync(file+'.partial')||fs.existsSync(file+'.json'))throw Error('请选择不存在的新备份文件');
 let session=await openDfu(1000);if(!session){await kick();session=await openDfu(40000);}if(!session)throw Error('未找到屏幕DFU；请USB直连键盘并检查驱动');
 const D=mk(session);let result,error,leaveError;
 try{result=await backupToFile(D,file,(done,total)=>log('备份（双读校验）',done+'/'+total));}
 catch(e){error=e;}
 finally{try{await leave(D);}catch(e){leaveError=e;}closeDfu(session);}
 if(error)throw Error(error.message+'；不完整数据保留在.partial；'+(leaveError?'未确认退出DFU：'+leaveError.message:'已发出回应用请求'));
 result.appReturned=false;
 if(!leaveError){const HID=require('node-hid');for(let i=0;i<30;i++){await sleep(200);try{if(HID.devices().some(d=>d.vendorId===0x5542&&d.productId===1)){result.appReturned=true;break;}}catch{}}}
 if(!result.appReturned)result.warning='备份完整，但尚未确认屏幕回到应用模式，请保留日志并检查连接。';
 result.firmwareKind=firmwareKind(fs.readFileSync(file));
 fs.writeFileSync(file+'.json',JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));return result;
}
module.exports={backupToFile,main,BASE,LENGTH};
if(require.main===module)main(process.argv.slice(2)).catch(e=>{console.error(e.message);process.exitCode=1;});
