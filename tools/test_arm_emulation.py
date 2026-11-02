from pathlib import Path
import struct,json,sys
from unicorn import Uc,UC_ARCH_ARM,UC_MODE_THUMB,UC_HOOK_CODE
from unicorn.arm_const import *
from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB
from build_fullscreen import SOURCE,patch
original=SOURCE.read_bytes();target,manifest=patch(original)
md=Cs(CS_ARCH_ARM,CS_MODE_THUMB)
def emulate(image,entry,r0,r1=0,r2=0):
 u=Uc(UC_ARCH_ARM,UC_MODE_THUMB);u.mem_map(0,0x100000);u.mem_write(0x8000,image);u.mem_map(0x20000000,0x100000)
 u.reg_write(UC_ARM_REG_SP,0x200ff000);u.reg_write(UC_ARM_REG_LR,0x70001)
 for reg,val in [(UC_ARM_REG_R0,r0),(UC_ARM_REG_R1,r1),(UC_ARM_REG_R2,r2)]:u.reg_write(reg,val)
 calls=[];objects=[]
 def hook(uc,a,size,data):
  if a==0x70000:uc.emu_stop();return
  ins=next(md.disasm(bytes(uc.mem_read(a,size)),a))
  if ins.mnemonic=='bl':
   dest=int(ins.op_str.removeprefix('#'),0);regs=[uc.reg_read(k) for k in [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3]];calls.append((a,dest,regs))
   if dest in [0x26dd4,0x2412c,0x25430]:
    ptr=0x20020000+len(objects)*0x100;objects.append((ptr,regs[0]));uc.reg_write(UC_ARM_REG_R0,ptr)
   elif dest==0x83e4:
    uc.mem_write(regs[0],bytes(uc.mem_read(regs[1],regs[2])))
   elif dest==0x1fdcc:uc.reg_write(UC_ARM_REG_R0,0)
   uc.reg_write(UC_ARM_REG_PC,(a+size)|1)
 u.hook_add(UC_HOOK_CODE,hook);u.emu_start(entry|1,0x70000,count=10000)
 assert u.reg_read(UC_ARM_REG_PC)==0x70000
 return u,calls,objects
summaries=[]
for image,expectedxy,expectedsize in [(original,[140,20],[320,320]),(target,[0,0],[480,360])]:
 u,calls,objs=emulate(image,0x376d0,0x20010000)
 parent=struct.unpack('<I',u.mem_read(0x200100d8,4))[0];static=struct.unpack('<I',u.mem_read(0x200100dc,4))[0];side=struct.unpack('<I',u.mem_read(0x200100d4,4))[0]
 positions=[c[2][1:3] for c in calls if c[1]==0x2a6e8 and c[2][0]==parent];sizes=[c[2][1:3] for c in calls if c[1]==0x2a770 and c[2][0]==parent];staticsizes=[c[2][1:3] for c in calls if c[1]==0x2a770 and c[2][0]==static]
 assert positions==[expectedxy] and sizes==[expectedsize] and staticsizes==[expectedsize]
 assert dict(objs)[static]==parent
 flags=[c[2][1] for c in calls if c[1]==0x26798 and c[2][0]==side];assert flags==([2] if image is original else [3])
 info,_,_=emulate(image,0x8c70,0,0x20030000,32);payload=bytes(info.mem_read(0x20030000,17));assert struct.unpack_from('<HH',payload,4)==tuple(expectedsize)
 summaries.append(dict(position=positions[0],container_size=sizes[0],static_size=staticsizes[0],info_payload=payload.hex(),sidebar_flags=flags[0]))
# Execute the real MOVW/MOVT instructions independently in ARM emulation.
for lo,hi,reg,value in [(0x11928,0x1192c,UC_ARM_REG_R0,345608),(0x11a38,0x11a40,UC_ARM_REG_R0,345608),(0x33912,0x3391a,UC_ARM_REG_R10,345608),(0x339ce,0x339d4,UC_ARM_REG_R5,345608),(0x11996,0x1199e,UC_ARM_REG_R6,172804),(0x339b4,0x339b8,UC_ARM_REG_R2,172804)]:
 u=Uc(UC_ARCH_ARM,UC_MODE_THUMB);u.mem_map(0,0x100000);u.mem_write(0x8000,target)
 for a in [lo,hi]:u.emu_start(a|1,a+4,count=1)
 assert u.reg_read(reg)==value
# Every changed payload byte is covered by the explicit patch manifest; entry vectors and all others stay identical.
allowed=set()
for p in manifest['patches']:
 o=int(p['offset'],16);allowed.update(range(o,o+len(bytes.fromhex(p['after']))))
assert all(a==b or i in allowed for i,(a,b) in enumerate(zip(original,target[:len(original)])))
try:patch(original[:-1]+b'\0')
except ValueError:pass
else:raise AssertionError('wrong hash accepted')
print(json.dumps({'result':'PASS','layout_and_info_before_after':summaries,'capacity_pairs_executed':6,'exact_payload_changed_bytes':manifest['changed_bytes'],'changed_sectors':manifest['sectors'],'limits':'BL外部调用用桩；不模拟LCD、硬件JPEG解码、Flash或真实堆余量'},ensure_ascii=False,indent=2))

