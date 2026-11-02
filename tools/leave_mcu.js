const {openDfu,mk,closeDfu}=require('./mcu_dfu');
(async()=>{const s=await openDfu(8000);if(!s)throw Error('DFU absent');const D=mk(s);try{await D.toIdle();await D.setAddr(0x8000);await D.cout(1,new Uint8Array(0),0);console.log('manifest',await D.settle());}finally{closeDfu(s);}})().catch(e=>{console.error(e);process.exitCode=1;});
