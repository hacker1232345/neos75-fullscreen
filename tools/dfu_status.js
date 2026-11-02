const {openDfu,mk,closeDfu}=require('./mcu_dfu');
(async()=>{const s=await openDfu(8000);if(!s)throw Error('DFU absent');const d=mk(s);try{console.log('STATUS before',await d.status());await d.toIdle();console.log('STATUS idle',await d.status());}finally{closeDfu(s);}})().catch(e=>{console.error(e);process.exitCode=1;});
