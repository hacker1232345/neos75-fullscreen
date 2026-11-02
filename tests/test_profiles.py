from pathlib import Path
import hashlib,importlib.util,json,tempfile,unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('profile_builder',ROOT/'tools/build_fullscreen.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class AutomaticProfiles(unittest.TestCase):
    def test_both_profiles_route_without_force_and_reject_modified_code(self):
        # Synthetic code hashes exercise the actual catalog loader; no device dump is committed.
        real=b.manifests();fixtures=[]
        with tempfile.TemporaryDirectory() as folder:
            temp=Path(folder);(temp/'firmware/profiles').mkdir(parents=True)
            for index,m in enumerate(real):
                source=bytearray([0x61+index]*m['payload_bytes'])
                for p in m['patches']:
                    o=int(p['offset'],16);old=bytes.fromhex(p['before']);source[o:o+len(old)]=old
                target=bytearray(source)
                for p in m['patches']:
                    o=int(p['offset'],16);new=bytes.fromhex(p['after']);target[o:o+len(new)]=new
                m={**m,'source_code_sha256':hashlib.sha256(source[:487424]).hexdigest(),'target_code_sha256':hashlib.sha256(target[:487424]).hexdigest()}
                dest=temp/('firmware/fullscreen.patch.json' if index==0 else 'firmware/profiles/issue1.patch.json')
                dest.write_text(json.dumps(m),encoding='utf-8');fixtures.append((bytes(source),bytes(target),m))
            with patch.object(b,'ROOT',temp),patch.object(b,'MANIFEST',temp/'firmware/fullscreen.patch.json'):
                for source,target,m in fixtures:
                    self.assertEqual(b.firmware_info(source)['profile'],m['profile'])
                    image,actual=b.patch(source)
                    self.assertEqual(image[:len(source)],target)
                    self.assertEqual(actual['profile'],m['profile']);self.assertFalse(actual['force'])
                    self.assertEqual(b.firmware_info(target)['firmwareKind'],'already-fullscreen')
                    with self.assertRaises(ValueError):b.patch(target)
                    tail=source[:-1]+bytes([source[-1]^1]);variant,_=b.patch(tail)
                    self.assertEqual(variant[487424:491520],tail[487424:])
                    changed=bytearray(source);changed[256]^=1
                    self.assertEqual(b.firmware_info(changed)['firmwareKind'],'unsupported-version')
                    with self.assertRaises(ValueError):b.patch(changed)
                self.assertNotEqual(fixtures[0][2]['source_code_sha256'],fixtures[1][2]['source_code_sha256'])
                self.assertNotEqual(fixtures[0][2]['patches'],fixtures[1][2]['patches'])

    def test_profile_catalog_consistency(self):
        profiles=b.manifests();self.assertEqual(len(profiles),2)
        self.assertEqual(len({m['profile'] for m in profiles}),2)
        hashes=[m[k] for m in profiles for k in ['source_code_sha256','target_code_sha256']]
        self.assertEqual(len(set(hashes)),4)
        for m in profiles:
            changes={}
            for p in m['patches']:
                o=int(p['offset'],16)
                for index,(old,new) in enumerate(zip(bytes.fromhex(p['before']),bytes.fromhex(p['after']))):
                    if old!=new:changes[o+index]=True
            self.assertEqual(len(changes),m['changed_bytes'])
            self.assertEqual(sorted({hex(0x8000+o//4096*4096) for o in changes}),m['sectors'])
        self.assertEqual(profiles[0]['validation'],'hardware-verified')
        self.assertEqual(profiles[1]['validation'],'offline-validated-awaiting-hardware')

if __name__=='__main__':unittest.main()
