'use strict';
const {backupToFile,BASE,LENGTH}=require('./mcu_backup'),fs=require('fs'),os=require('os'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
(async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'neos75-backup-test-'));
 try{
  const image=Buffer.from(Array.from({length:LENGTH},(_,i)=>(i*13+7)&255)),file=path.join(dir,'backup.bin');let reads=0;
  const D={read:async(a,n)=>{reads++;assert(n===64&&a>=BASE&&a+n<=BASE+LENGTH);return Buffer.from(image.subarray(a-BASE,a-BASE+n));}};
  const result=await backupToFile(D,file);assert.equal(reads,LENGTH/64*2);assert.deepEqual(fs.readFileSync(file),image);assert.equal(result.sha256,crypto.createHash('sha256').update(image).digest('hex'));assert.equal(result.flashWritten,false);
  await assert.rejects(backupToFile(D,file),/已存在/);
  const short=path.join(dir,'short.bin');await assert.rejects(backupToFile({read:async()=>Buffer.alloc(63)},short),/短读/);assert(!fs.existsSync(short)&&fs.existsSync(short+'.partial'));
  let counter=0;const changed=path.join(dir,'changed.bin');await assert.rejects(backupToFile({read:async()=>Buffer.alloc(64,counter++%2)},changed),/不一致/);assert(!fs.existsSync(changed));
  console.log('PASS: baseline-free complete backup, double-read equality, exact disk SHA256, short-read rejection, mismatch rejection, no overwrite, no erase/program API used');
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
