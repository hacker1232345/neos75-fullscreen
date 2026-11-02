from pathlib import Path
import importlib.util,json,unittest,subprocess,zlib,struct
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('builder',ROOT/'tools/build_fullscreen.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class PatchTests(unittest.TestCase):
    def test_forced_unknown_image_preserves_other_bytes_and_matches_node(self):
        m=json.loads(b.MANIFEST.read_text(encoding='utf-8'))
        source=bytearray(b'\xa5'*m['payload_bytes'])
        for p in m['patches']:
            o=int(p['offset'],16);old=bytes.fromhex(p['before']);source[o:o+len(old)]=old
        source[int(m['patches'][0]['offset'],16)]^=0x80
        with self.assertRaises(ValueError):b.patch(bytes(source))
        image,metadata=b.patch(bytes(source),force=True)
        expected=bytearray(source)
        for p in m['patches']:
            o=int(p['offset'],16);new=bytes.fromhex(p['after']);expected[o:o+len(new)]=new
        self.assertEqual(image[:len(source)],expected)
        self.assertEqual(image[:256],source[:256])
        self.assertEqual(image[m['immutable_payload_bytes']:len(source)],source[m['immutable_payload_bytes']:])
        self.assertTrue(metadata['force'])
        self.assertEqual(metadata['changed_bytes'],sum(x!=y for x,y in zip(source,expected)))
        self.assertEqual(image[-4:],struct.pack('<I',(~zlib.crc32(image[:-4]))&0xffffffff))
        # Avoid command line length limits: pass the fixture on stdin instead.
        command="const f=require('./tools/firmware_image'),fs=require('fs');let s=fs.readFileSync(0);process.stdout.write(f.buildFirmware(s,true))"
        node=subprocess.run(['node','-e',command],input=bytes(source),capture_output=True,cwd=ROOT,check=True)
        self.assertEqual(node.stdout,image)
        with self.assertRaises(ValueError):b.patch(bytes(source[:-1]),force=True)

    def test_wrong_device_backup_is_rejected(self):
        with self.assertRaises(ValueError):b.patch(bytes(491520))
        with self.assertRaises(ValueError):b.patch(b'')
    def test_declared_byte_changes_and_sector_scope(self):
        m=json.loads(b.MANIFEST.read_text(encoding='utf-8'));changed={};seen=set()
        for p in m['patches']:
            o=int(p['offset'],16);old=bytes.fromhex(p['before']);new=bytes.fromhex(p['after'])
            self.assertEqual(len(old),len(new));self.assertGreaterEqual(o,256)
            for i,(x,y) in enumerate(zip(old,new)):
                self.assertNotIn(o+i,seen);seen.add(o+i)
                if x!=y:changed[o+i]=(x,y)
        self.assertEqual(len(changed),27)
        self.assertEqual(sorted({hex(0x8000+(o//4096)*4096) for o in changed}),sorted(m['sectors']))

if __name__=='__main__':unittest.main()
