from pathlib import Path
import importlib.util,json,unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('builder',ROOT/'tools/build_fullscreen.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class PatchTests(unittest.TestCase):
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
