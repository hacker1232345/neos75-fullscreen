from pathlib import Path
import hashlib,importlib.util,json,struct,tempfile,unittest,zlib
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('profile_builder',ROOT/'tools/build_fullscreen.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

CODE=487424
def off(p):return int(p['offset'],16)
def raw(p,k):return bytes.fromhex(p[k])
def changed_bytes(m):return sum(1 for p in m['patches'] for x,y in zip(raw(p,'before'),raw(p,'after')) if x!=y)

def build(ref,clean,fill=0x61):
    """造三层合成镜像：原版 → 全屏 → 全屏+清爽。

    关键：原版必须同时满足 reference 的 before 与 clean 的 before
    （两层偏移互不重叠），否则链式应用会在第二层报「旧字节不匹配」。
    """
    origin=bytearray([fill]*ref['payload_bytes'])
    for m,key in ((ref,'before'),(clean,'before')):
        for p in m['patches']:origin[off(p):off(p)+len(raw(p,key))]=raw(p,key)
    fullscreen=bytearray(origin)
    for p in ref['patches']:fullscreen[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
    enhanced=bytearray(fullscreen)
    for p in clean['patches']:enhanced[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
    return bytes(origin),bytes(fullscreen),bytes(enhanced)

def code(x):return hashlib.sha256(x[:CODE]).hexdigest()

class ChainProfiles(unittest.TestCase):
    """补丁链：链首可打第一层、链中可继续升级、链末无可再打；只认哈希衔接。"""

    def setUp(self):
        real=b.manifests()
        self.ref=next(m for m in real if m['profile']=='reference-66679ff2')
        self.clean=next(m for m in real if m['profile']=='clean-8465dd63')
        self.issue=next(m for m in real if m['profile']=='issue-1-74f2fa47')
        # 两层偏移必须互不重叠，否则「逐层」与「一次性」的等价性不成立
        seen=set()
        for m in (self.ref,self.clean):
            for p in m['patches']:
                for i in range(len(raw(p,'before'))):
                    self.assertNotIn(off(p)+i,seen,'两层补丁偏移重叠 '+p['address'])
                    seen.add(off(p)+i)
        self.origin,self.fullscreen,self.enhanced=build(self.ref,self.clean)
        self.refm={**self.ref,'source_code_sha256':code(self.origin),
                             'target_code_sha256':code(self.fullscreen),
                             'changed_bytes':changed_bytes(self.ref)}
        self.cleanm={**self.clean,'source_code_sha256':code(self.fullscreen),
                                 'target_code_sha256':code(self.enhanced),
                                 'changed_bytes':changed_bytes(self.clean)}
        self.issue_payload=bytearray([0x7a]*self.issue['payload_bytes'])
        for p in self.issue['patches']:self.issue_payload[off(p):off(p)+len(raw(p,'before'))]=raw(p,'before')
        issue_tgt=bytearray(self.issue_payload)
        for p in self.issue['patches']:issue_tgt[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
        self.issuem={**self.issue,'source_code_sha256':code(bytes(self.issue_payload)),
                                 'target_code_sha256':code(bytes(issue_tgt)),
                                 'changed_bytes':changed_bytes(self.issue)}
        self.tmp=tempfile.TemporaryDirectory();folder=Path(self.tmp.name)
        (folder/'firmware/profiles').mkdir(parents=True)
        (folder/'firmware/fullscreen.patch.json').write_text(json.dumps(self.refm),encoding='utf-8')
        (folder/'firmware/profiles/clean.patch.json').write_text(json.dumps(self.cleanm),encoding='utf-8')
        (folder/'firmware/profiles/issue1.patch.json').write_text(json.dumps(self.issuem),encoding='utf-8')
        self.stack=[patch.object(b,'ROOT',folder),patch.object(b,'MANIFEST',folder/'firmware/fullscreen.patch.json')]
        for s in self.stack:s.start()

    def tearDown(self):
        for s in self.stack:s.stop()
        self.tmp.cleanup()

    def test_first_layer_entry_reaches_chain_leaf(self):
        info=b.firmware_info(self.origin)
        self.assertEqual(info['firmwareKind'],'supported-original')
        self.assertTrue(info['patchable'])
        self.assertEqual(info['profile'],self.ref['profile'])
        self.assertEqual(info['validation'],'hardware-verified')
        self.assertEqual(info['pending'],[self.ref['label'],self.clean['label']])
        image,m=b.patch(self.origin)
        self.assertEqual(image[:len(self.origin)],self.enhanced)
        self.assertEqual(m['appliedProfiles'],[self.ref['profile'],self.clean['profile']])
        self.assertEqual(m['changed_bytes'],self.refm['changed_bytes']+self.cleanm['changed_bytes'])
        self.assertEqual(image[:256],self.origin[:256])
        self.assertEqual(image[-4:],struct.pack('<I',(~zlib.crc32(image[:-4]))&0xffffffff))

    def test_mid_chain_entry_applies_only_remaining_layer(self):
        info=b.firmware_info(self.fullscreen)
        self.assertEqual(info['firmwareKind'],'upgradeable')
        self.assertTrue(info['patchable'])
        self.assertEqual(info['profile'],self.ref['profile'])
        self.assertEqual(info['pending'],[self.clean['label']])
        self.assertEqual(info['upgradeProfile'],self.clean['profile'])
        self.assertEqual(info['upgradeLabel'],self.clean['label'])
        self.assertEqual(info['upgradeValidation'],'hardware-verified')
        image,m=b.patch(self.fullscreen)
        self.assertEqual(image[:len(self.fullscreen)],self.enhanced)
        self.assertEqual(m['appliedProfiles'],[self.clean['profile']])
        self.assertEqual(m['changed_bytes'],self.cleanm['changed_bytes'])

    def test_leaf_has_nothing_left(self):
        info=b.firmware_info(self.enhanced)
        self.assertEqual(info['firmwareKind'],'already-fullscreen')
        self.assertFalse(info['patchable'])
        self.assertEqual(info['pending'],[])
        with self.assertRaises(ValueError):b.patch(self.enhanced)

    def test_layerwise_equals_one_shot(self):
        one_shot,_=b.patch(self.origin)
        again,m2=b.patch(self.fullscreen)
        self.assertEqual(one_shot[:len(self.origin)],again[:len(self.fullscreen)])
        self.assertEqual(m2['appliedProfiles'],[self.clean['profile']])
        self.assertEqual(m2['target_sha256'],hashlib.sha256(one_shot).hexdigest())

    def test_tail_preserved_from_both_entries(self):
        for entry in (self.origin,self.fullscreen):
            tail=bytearray(entry);tail[CODE+7]^=1;tail=bytes(tail)
            image,_=b.patch(tail)
            self.assertEqual(image[CODE:len(tail)],tail[CODE:])
            self.assertEqual(image[:CODE],self.enhanced[:CODE])

    def test_corrupt_and_unknown_rejected(self):
        wrong=bytearray(self.origin);wrong[256]^=1
        self.assertEqual(b.firmware_info(bytes(wrong))['firmwareKind'],'unsupported-version')
        self.assertFalse(b.firmware_info(bytes(wrong))['patchable'])
        with self.assertRaises(ValueError):b.patch(bytes(wrong))
        with self.assertRaises(ValueError):b.patch(bytes(491520))
        with self.assertRaises(ValueError):b.patch(b'')
        with self.assertRaises(ValueError):b.patch(self.origin[:-1],force=True)

    def test_force_still_bounded_by_leaf_target(self):
        junk=bytes([0x5a])*self.ref['payload_bytes']
        image,m=b.patch(junk,force=True)
        self.assertTrue(m['force'])
        self.assertEqual(m['appliedProfiles'],[self.ref['profile']])
        expected=bytearray(junk)
        for p in self.ref['patches']:expected[off(p):off(p)+len(raw(p,'after'))]=raw(p,'after')
        self.assertEqual(image[:len(junk)],bytes(expected))

class CatalogConsistency(unittest.TestCase):
    def test_catalog_shape(self):
        profiles=b.manifests()
        self.assertEqual(len(profiles),3)
        self.assertEqual(len({m['profile'] for m in profiles}),3)
        for m in profiles:
            changes=set()
            for p in m['patches']:
                o=off(p)
                self.assertEqual(len(raw(p,'before')),len(raw(p,'after')),p['address'])
                for i,(x,y) in enumerate(zip(raw(p,'before'),raw(p,'after'))):
                    if x!=y:changes.add(o+i)
            self.assertEqual(len(changes),m['changed_bytes'],m['profile'])
            self.assertEqual(sorted({hex(0x8000+o//4096*4096) for o in changes}),m['sectors'],m['profile'])
            self.assertGreater(m['immutable_payload_bytes'],max(off(p)+len(raw(p,'after')) for p in m['patches']))
            self.assertEqual(m['payload_bytes']-m['immutable_payload_bytes'],m['preserved_tail_bytes'])
            self.assertTrue(m['dfu_suffix_prefix_hex'].startswith('ffff'))

    def test_chain_links_by_hash_not_by_parent_label(self):
        profiles={m['profile']:m for m in b.manifests()}
        ref,clean=profiles['reference-66679ff2'],profiles['clean-8465dd63']
        # 链衔接靠哈希：clean 的 source 必须正好是 reference 的 target（因此只有 5 个唯一哈希）
        self.assertEqual(clean['source_code_sha256'],ref['target_code_sha256'])
        self.assertEqual(clean['parent'],ref['profile'])
        self.assertNotEqual(clean['target_code_sha256'],ref['target_code_sha256'])
        hashes=[m[k] for m in profiles.values() for k in ['source_code_sha256','target_code_sha256']]
        self.assertEqual(len(set(hashes)),5)
        # clean 层不得与 reference 层改动同一个字节
        ref_bytes={off(p)+i for p in ref['patches'] for i in range(len(raw(p,'before')))}
        clean_bytes={off(p)+i for p in clean['patches'] for i in range(len(raw(p,'before')))}
        self.assertEqual(ref_bytes&clean_bytes,set())
        # Issue #1 链路没有 clean 层，且它是独立根（没有 parent），不得套用 reference 的偏移
        issue=profiles['issue-1-74f2fa47']
        self.assertNotEqual(issue['target_code_sha256'],clean['source_code_sha256'])
        self.assertNotEqual(issue['source_code_sha256'],ref['target_code_sha256'])
        self.assertNotIn('parent',issue)
        self.assertEqual(b.chain_depth(issue),0)
        self.assertEqual(b.chain_of(issue),[issue])
        issue_bytes={off(p)+i for p in issue['patches'] for i in range(len(raw(p,'before')))}
        self.assertEqual(issue_bytes&clean_bytes,set())
        self.assertEqual({m['validation'] for m in profiles.values()},
                         {'hardware-verified','offline-validated-awaiting-hardware'})

if __name__=='__main__':unittest.main()
