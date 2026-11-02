from pathlib import Path
import importlib.util,json,struct,subprocess,unittest,zlib
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('builder',ROOT/'tools/build_fullscreen.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

def off(p):return int(p['offset'],16)
def raw(p,k):return bytes.fromhex(p[k])
def changed_offsets(m):
    out={}
    for p in m['patches']:
        for i,(x,y) in enumerate(zip(raw(p,'before'),raw(p,'after'))):
            if x!=y:out[off(p)+i]=(x,y)
    return out

class PatchTests(unittest.TestCase):
    """第一层（原版→全屏）与第二层（全屏→全屏+清爽）都用真实记录做端到端一致性校验。"""

    def setUp(self):
        real=b.manifests()
        self.ref=next(m for m in real if m['profile']=='reference-66679ff2')
        self.clean=next(m for m in real if m['profile']=='clean-8465dd63')
        self.code=self.ref['immutable_payload_bytes']

    def _force_source(self,m,fill=0xa5):
        source=bytearray([fill]*m['payload_bytes'])
        for p in m['patches']:source[off(p):off(p)+len(raw(p,'before'))]=raw(p,'before')
        return source

    def test_forced_unknown_image_preserves_other_bytes_and_matches_node(self):
        m=self.ref
        source=self._force_source(m)
        source[off(m['patches'][0])]^=0x80
        with self.assertRaises(ValueError):b.patch(bytes(source))
        image,metadata=b.patch(bytes(source),force=True)
        expected=bytearray(source)
        for p in m['patches']:expected[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
        self.assertEqual(image[:len(source)],bytes(expected))
        self.assertEqual(image[:256],source[:256])
        self.assertEqual(image[self.code:len(source)],source[self.code:])
        self.assertTrue(metadata['force'])
        self.assertEqual(metadata['changed_bytes'],sum(x!=y for x,y in zip(source,expected)))
        self.assertEqual(image[-4:],struct.pack('<I',(~zlib.crc32(image[:-4]))&0xffffffff))
        # 强制模式下未命中任何 source 哈希时只套用链首列表 [manifest]
        self.assertEqual(metadata['appliedProfiles'],[self.ref['profile']])
        # Avoid command line length limits: pass the fixture on stdin instead.
        command="const f=require('./tools/firmware_image'),fs=require('fs');let s=fs.readFileSync(0);process.stdout.write(f.buildFirmware(s,true))"
        node=subprocess.run(['node','-e',command],input=bytes(source),capture_output=True,cwd=ROOT,check=True)
        self.assertEqual(node.stdout,image)
        with self.assertRaises(ValueError):b.patch(bytes(source[:-1]),force=True)

    def test_second_layer_forced_application_keeps_first_layer_bytes(self):
        """clean 层套在 reference 结果之上：链首字节必须原样保留，且只改 clean 声明的字节。"""
        source=bytearray(self._force_source(self.ref))
        for p in self.ref['patches']:source[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
        for p in self.clean['patches']:source[off(p):off(p)+len(raw(p,'before'))]=raw(p,'before')
        for i in [off(p) for p in self.clean['patches']]:source[i]^=0x40   # 让哈希不可识别，强制走 force
        image,metadata=b.patch(bytes(source),force=True)
        expected=bytearray(source)
        for p in self.ref['patches']:expected[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
        self.assertEqual(image[:len(source)],bytes(expected))
        self.assertEqual(metadata['appliedProfiles'],[self.ref['profile']])
        command="const f=require('./tools/firmware_image'),fs=require('fs');let s=fs.readFileSync(0);process.stdout.write(f.buildFirmware(s,true))"
        node=subprocess.run(['node','-e',command],input=bytes(source),capture_output=True,cwd=ROOT,check=True)
        self.assertEqual(node.stdout,image)

    def test_wrong_device_backup_is_rejected(self):
        with self.assertRaises(ValueError):b.patch(bytes(491520))
        with self.assertRaises(ValueError):b.patch(b'')

    def test_declared_byte_changes_and_sector_scope(self):
        for m in (self.ref,self.clean):
            changed=changed_offsets(m);seen=set()
            for p in m['patches']:
                o=off(p)
                self.assertEqual(len(raw(p,'before')),len(raw(p,'after')))
                self.assertGreaterEqual(o,256)
                for i in range(len(raw(p,'before'))):
                    self.assertNotIn(o+i,seen);seen.add(o+i)
            self.assertEqual(len(changed),m['changed_bytes'],m['profile'])
            self.assertEqual(sorted({hex(0x8000+(o//4096)*4096) for o in changed}),sorted(m['sectors']),m['profile'])

    def test_layers_never_touch_the_same_byte(self):
        """两层偏移互不重叠 —— 这是「逐层应用」与「一次性应用」等价的充分条件。"""
        a=set(changed_offsets(self.ref));c=set(changed_offsets(self.clean))
        self.assertEqual(a&c,set())
        self.assertEqual(len(a),self.ref['changed_bytes'])
        self.assertEqual(len(c),self.clean['changed_bytes'])
        self.assertEqual(len(a)+len(c),len(a|c))

if __name__=='__main__':unittest.main()
