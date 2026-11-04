from pathlib import Path
import importlib.util
import json
import random
import hashlib
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f'{name}.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A = load('adapt_firmware')
I = load('adapt_issue')

ANCHOR_DIR = ROOT / 'firmware' / 'anchors'
CODE = 487424
PAYLOAD = 491520


class AnchorFiles(unittest.TestCase):
    """锚点文件必须能把「每一条补丁」都覆盖到，且格式统一。"""

    def test_every_anchor_file_matches_a_declared_patch(self):
        for p in sorted(ANCHOR_DIR.glob('*.json')):
            if p.name == 'symbols.json':
                continue
            doc = json.loads(p.read_text(encoding='utf-8'))
            layer = next(L for L in A.layers() if L['profile'] == doc['profile'])
            self.assertEqual(doc['total'], len(layer['patches']), p.name)
            offsets = {a['offset'] for a in doc['anchors']}
            self.assertEqual(offsets, {q['offset'] for q in layer['patches']},
                             f'{p.name} 的锚点没覆盖全部补丁')

    def test_anchored_ones_carry_a_useable_fingerprint(self):
        for p in sorted(ANCHOR_DIR.glob('*.json')):
            if p.name == 'symbols.json':
                continue
            doc = json.loads(p.read_text(encoding='utf-8'))
            for a in doc['anchors']:
                if not a.get('anchor'):
                    # 注入层故意不锚定：那段代码含绝对地址，换固件必须重新汇编
                    self.assertIn('空闲带', a.get('reason') or '', p.name)
                    continue
                blob = bytes.fromhex(a['anchor'])
                self.assertGreaterEqual(len(blob), 2, p.name)
                # 锚点窗口不能盖住自己这条补丁的**被改部分**，否则打完补丁就搜不到了
                start = int(a['offset'], 16) + a['anchor_delta']
                end = start + len(blob)
                self.assertFalse(start <= int(a['offset'], 16) < end,
                                 f'{p.name} 的 {a["offset"]} 锚点盖住了自己')


class SymbolFile(unittest.TestCase):
    def test_covers_eleven_functions_and_two_structure_points(self):
        doc = json.loads((ANCHOR_DIR / 'symbols.json').read_text(encoding='utf-8'))
        funcs = dict(doc['functions'])
        stage = {'MEMCPY', 'LBL_CREATE', 'LBL_TEXT', 'ALLOC', 'SET_POS', 'SET_SIZE',
                 'SET_COLOR', 'SET_FONT', 'GETSCREEN', 'TIMER', 'FONT'}
        self.assertEqual(set(funcs) & stage, stage, '少了厂商函数签名')
        self.assertEqual(set(funcs) & {'TBB', 'CMD_RET'}, {'TBB', 'CMD_RET'})
        for k, v in funcs.items():
            blob = bytes.fromhex(v['signature_hex'])
            self.assertEqual(len(blob), doc['signature_bytes'], k)
            self.assertEqual(int(v['address'], 16) % 2 == 0 if isinstance(v['address'], str)
                             else v['address'] % 2 == 0, True, f'{k} 地址没对齐')

    def test_signature_length_is_at_least_sixteen(self):
        doc = json.loads((ANCHOR_DIR / 'symbols.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(doc['signature_bytes'], 16)


class Locate(unittest.TestCase):
    """定位：唯一命中才算数，重复命中一律拒绝。"""

    def setUp(self):
        # 用伪随机填充，保证任意 8 字节窗口基本都只出现一次 —— 「唯一」得靠数据，
        # 不能靠巧合（早先用了 range(256) 重复，8 字节锚点命中 3 次）。
        self.img = random.Random(7).randbytes(4096)

    def test_unique_anchor_maps_back_to_its_offset(self):
        blob = self.img[300:308]
        self.assertEqual(A.counts(self.img, blob), 1)
        got, why = A.locate(self.img, {'anchor': blob.hex(), 'anchor_delta': 4})
        self.assertEqual((got, why), (296, None))

    def test_repeated_anchor_is_refused(self):
        blob = self.img[100:108]              # 故意在别处再放一份一模一样的
        self.img = self.img + blob
        self.assertEqual(A.counts(self.img, blob), 2)
        got, why = A.locate(self.img, {'anchor': blob.hex(), 'anchor_delta': 0})
        self.assertIsNone(got)
        self.assertIn('不再唯一', why)

    def test_missing_anchor_says_so(self):
        got, why = A.locate(self.img, {'anchor': 'fefefefefefefefe', 'anchor_delta': 0})
        self.assertIsNone(got)
        self.assertIn('找不到', why)


class PlanLayer(unittest.TestCase):
    """三种结局：原位命中 / 按锚点搬移 / 找到位置但字节不对。"""

    def setUp(self):
        self.layer = {'profile': 'demo', 'payload_bytes': 4096,
                      'patches': [{'offset': '0x40', 'purpose': 'x',
                                   'before': 'aabbccdd', 'after': '11223344'}]}
        self.base = bytearray(random.Random(11).randbytes(4096))
        self.base[0x40:0x44] = b'\xaa\xbb\xcc\xdd'
        # 锚点取补丁**前面** 6 字节，不含被改的部分
        self.doc = {'profile': 'demo', 'anchors': [
            {'offset': '0x40', 'before': 'aabbccdd',
             'anchor': bytes(self.base[0x3a:0x40]).hex(), 'anchor_delta': -6}]}

    def test_original_bytes_need_no_moving(self):
        entries, blockers = A.plan_layer(bytes(self.base), self.layer, self.doc)
        self.assertEqual(blockers, [])
        self.assertEqual(entries[0]['anchor'], 'original')
        self.assertEqual(entries[0]['offset'], '0x40')

    def test_shifted_image_is_located_by_content(self):
        img = bytearray(random.Random(12).randbytes(0x2060))
        img[0x2040:0x2044] = b'\xaa\xbb\xcc\xdd'
        doc = {'profile': 'demo', 'anchors': [
            {'offset': '0x40', 'before': 'aabbccdd',
             'anchor': bytes(img[0x203a:0x2040]).hex(), 'anchor_delta': -6}]}
        self.assertEqual(A.counts(bytes(img), bytes.fromhex(doc['anchors'][0]['anchor'])), 1)
        entries, blockers = A.plan_layer(bytes(img), self.layer, doc)
        self.assertEqual(blockers, [])
        self.assertEqual(entries[0]['anchor'], 'content')
        self.assertEqual(entries[0]['offset'], hex(0x2040))
        self.assertEqual(entries[0]['shift'], 0x2000)

    def test_found_but_wrong_bytes_is_a_mismatch_not_a_write(self):
        img = bytearray(random.Random(13).randbytes(0x2060))
        img[0x203a:0x2040] = random.Random(11).randbytes(4096)[0x3a:0x40]  # 锚点还在
        img[0x2040:0x2044] = b'\xde\xad\xbe\xef'                           # 补丁点被改过
        entries, blockers = A.plan_layer(bytes(img), self.layer, self.doc)
        self.assertEqual(entries, [])
        self.assertEqual(blockers[0]['kind'], 'mismatch', '找到了却字节不对，必须拒绝写入')
        self.assertEqual(blockers[0]['found_at'], hex(0x2040))

    def test_no_anchor_file_is_reported_as_missing_not_guessed(self):
        img = random.Random(14).randbytes(4096)   # 原位字节已经不在
        entries, blockers = A.plan_layer(img, self.layer, None)
        self.assertEqual(entries, [])
        self.assertEqual(blockers[0]['kind'], 'unlocatable')
        self.assertIn('还没有为这条补丁生成锚点', blockers[0]['reason'])


class IssueInput(unittest.TestCase):
    """issue 入口的护栏：长度不对、空白太多的都不许往下走。"""

    def test_accepts_both_known_lengths(self):
        body = bytes([0x33]) * PAYLOAD
        self.assertEqual(len(I.check(body)), PAYLOAD)
        self.assertEqual(len(I.check(body + bytes(16))), PAYLOAD)

    def test_rejects_other_lengths(self):
        for n in (PAYLOAD - 16, PAYLOAD + 32, 0, 272336, 2441516):
            with self.assertRaises(ValueError):
                I.check(bytes([0x33]) * n)

    def test_rejects_a_blank_backup(self):
        with self.assertRaises(ValueError):
            I.check(bytes([0xFF]) * PAYLOAD)

    def test_finds_a_bin_link_in_issue_text(self):
        text = ('键盘型号 Neos75 V2.0，备份在 '
                'https://github.com/user-attachments/assets/1111-2222 '
                '里，麻烦看看。')
        self.assertEqual(I.find_url(text),
                         'https://github.com/user-attachments/assets/1111-2222')

    def test_prefers_a_link_that_ends_in_bin(self):
        text = ('截图 https://example.com/a.png 固件 '
                'https://example.com/fw.bin?raw=1')
        self.assertIn('fw.bin', I.find_url(text))

    def test_no_link_at_all(self):
        self.assertIsNone(I.find_url('我传了固件，但是忘了贴链接'))


class BuilderWiring(unittest.TestCase):
    """桌面程序真正走的那条路：build_fullscreen.patch() 在哈希不认识时必须自己适配。

    v1.6.0 的自适应原本只做成了命令行工具，桌面程序照样抛「未找到对应固件配置」，
    用户被迫去勾「无验证」——于是进度条留在屏幕上。这一组用例就是钉死那件事。
    """

    def setUp(self):
        self.BF = load('build_fullscreen')

    def base(self, code_sha):
        for p in sorted(ROOT.parent.glob('widgetlab/*.bin')) + sorted(ROOT.parent.glob('mcu_out/*.bin')):
            if p.stat().st_size not in (PAYLOAD, PAYLOAD + 16):
                continue
            if hashlib.sha256(p.read_bytes()[:CODE]).hexdigest() == code_sha:
                return p.read_bytes()[:PAYLOAD]
        return None

    def shifted(self, img, at=0x9000, extra=0x400):
        """在代码区中间塞一段 0xFF，末尾挤出同样多 —— 模拟「换了一份固件，布局整体后移」。"""
        return (img[:at] + b'\xff' * extra + img[at:PAYLOAD - extra])[:PAYLOAD]

    def test_unknown_image_is_adapted_instead_of_refused(self):
        clean = next(L for L in A.layers() if L['profile'] == 'clean-8465dd63')
        src = self.base(clean['target_code_sha256'])
        if not src:
            self.skipTest('本机没有清爽版整片镜像')
        moved = self.shifted(src)
        self.assertNotEqual(hashlib.sha256(moved[:CODE]).hexdigest(),
                            hashlib.sha256(src[:CODE]).hexdigest())
        out, meta = self.BF.patch(moved, force=False)      # 关键：不是 force
        self.assertTrue(meta.get('adapted'), '未知固件必须走自动适配，而不是直接拒绝')
        self.assertEqual(len(out), PAYLOAD + 16)
        self.assertGreater(meta['changed_bytes'], 0)
        # 尾部现场数据不许动
        self.assertEqual(out[CODE:PAYLOAD], moved[CODE:PAYLOAD])

    def test_adapted_result_says_whether_the_progress_ring_is_gone(self):
        # 「去掉上传进度条」那一层在链中间。适配跳过它却仍说「适配成功」，
        # 用户刷完就会看到进度条还在 —— 这个字段就是用来在擦除前把话说准的。
        clean = next(L for L in A.layers() if L['profile'] == 'clean-8465dd63')
        src = self.base(clean['target_code_sha256'])
        if not src:
            self.skipTest('本机没有清爽版整片镜像')
        moved = self.shifted(src)
        out, meta = self.BF.patch(moved, force=False)
        self.assertIs(meta['progressRemoved'], True, '适配成功时进度条必须已经被去掉')

    def test_an_already_patched_image_says_so_instead_of_failing_inside(self):
        # 链末镜像的空闲带已被占用，再去跑适配器会抛内部错误（命令表项不符）。
        # 用户应该看到「已经是最新版」，而不是一句看不见懂的异常。
        q = self.base('ec412e092dc8a64278bf3f6c262296de204a1a8051bf3c0b518eda5032bd0154')
        if not q:
            self.skipTest('本机没有默认关闭版整片镜像')
        with self.assertRaises(ValueError) as ctx:
            self.BF.patch(q, force=False)
        msg = str(ctx.exception)
        self.assertIn('已是全屏版', msg)
        self.assertNotIn('0x3D', msg)

    def test_build_still_refuses_a_bogus_sized_backup(self):
        with self.assertRaises(ValueError):
            self.BF.patch(bytes(PAYLOAD - 8), force=False)

    def test_garbage_image_gets_a_readable_reason_not_a_stack(self):
        junk = random.Random(23).randbytes(PAYLOAD)
        with self.assertRaises(ValueError) as ctx:
            self.BF.patch(junk, force=False)
        msg = str(ctx.exception)
        self.assertNotIn('Traceback', msg)
        self.assertGreater(len(msg), 10)


class RealImage(unittest.TestCase):
    """有真机镜头的机器上跑端到端；没有就跳过，别让 CI 因为缺文件挂掉。"""

    def image(self, code_sha):
        """找一份代码指纹正好是 `code_sha` 的真机镜像；没有就返回 None。"""
        for p in sorted(ROOT.parent.glob('widgetlab/*.bin')) + sorted(ROOT.parent.glob('mcu_out/*.bin')):
            if p.stat().st_size not in (PAYLOAD, PAYLOAD + 16):
                continue
            if hashlib.sha256(p.read_bytes()[:CODE]).hexdigest() == code_sha:
                return p
        return None

    def test_mask_only_layer_turns_a_full_mask_build_into_the_off_build(self):
        want = next(L for L in A.layers() if L['profile'] == 'widget-dce6dce7')
        src = self.image(want['target_code_sha256'])
        if not src:
            self.skipTest('本机没有「覆盖组件全开」那份真机镜像（widgetlab/ov6p.bin）')
        data = src.read_bytes()[:PAYLOAD]
        out, rep, chain, meta = A.build_adapted(data, src.name)
        self.assertIsNotNone(meta, f'{src.name} 竟然没有需要打的层')
        self.assertEqual(meta['appliedProfiles'], ['widget-none-ec412e09'])
        self.assertEqual(meta['changed_bytes'], 366)
        off = next(L for L in A.layers() if L['profile'] == 'widget-none-ec412e09')
        self.assertEqual(hashlib.sha256(out[:CODE]).hexdigest(),
                         off['target_code_sha256'])
        # 尾部 4096 字节现场数据必须原样保留
        self.assertEqual(out[CODE:PAYLOAD], data[CODE:PAYLOAD])


if __name__ == '__main__':
    unittest.main()
