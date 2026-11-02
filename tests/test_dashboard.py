from pathlib import Path
import contextlib
import io
import importlib.util
import json
import re
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
DASH=ROOT/'dashboard'
spec=importlib.util.spec_from_file_location('dash_render',DASH/'render.py')
rd=importlib.util.module_from_spec(spec)
spec.loader.exec_module(rd)

W,H=480,360

STATE={'clock':'14:23:07','date':'2026-11-03','uptime':'3d 04:12','cpu_load':37.5,'cpu_freq_ghz':4.21,
 'mem_pct':61.2,'mem_used_gb':19.6,'mem_total_gb':32.0,'temp_c':58.0,'gpu_pct':12,'disk_free_gb':412.7,
 'net_down_mbps':3.21,'net_up_mbps':0.44,'bat_pct':82.0,'bat_charging':True,'bat_ac':True,'vol_pct':45,
 'top_proc':'chrome.exe','top_proc_pct':12.3,'ip':'192.168.1.42','host':'DESKTOP-NEOS75'}

REQUIRED_FIELDS=['id','label','desc','group','default','minWidth','minHeight','draw']
REQUIRED_IDS=['clock_big','date','cpu','mem','gpu','temp','disk','net','battery','uptime',
 'history_cpu','top_proc','volume','host','ip']
PRESET_NAMES=['default','minimal','full','performance','network']


def sof(jpeg):
    """walk JPEG 段，返回 (marker, precision, width, height)。

    SOF 段布局：FF <m> <len:2> <precision:1> <height:2> <width:2> —— 高度在前、宽度在后。
    首段必须是 0xC0 才是 baseline（0xC2 是 progressive，设备不接受）。
    """
    assert jpeg[:2]==b'\xff\xd8', '不是 JPEG（缺 SOI）'
    assert jpeg[-2:]==b'\xff\xd9', '不是 JPEG（缺 EOI）'
    p=2; first=None
    while p<len(jpeg):
        assert jpeg[p]==0xFF, 'JPEG 标记损坏 @%d'%p
        p+=1
        while jpeg[p]==0xFF:p+=1
        m=jpeg[p];p+=1
        if m==0xD9:break
        if m==0xDA:break
        n=int.from_bytes(jpeg[p:p+2],'big')
        if m in (0xC0,0xC1,0xC2):
            info=(m,jpeg[p+2],int.from_bytes(jpeg[p+5:p+7],'big'),int.from_bytes(jpeg[p+3:p+5],'big'))
            if first is None:first=info
        p+=n
    assert first is not None, 'JPEG 里找不到 SOF 段'
    return first


def main_args(argv, stdin=None):
    """跑 render.main()，返回 (exit_code, stdout, stderr)。"""
    out,err=io.StringIO(),io.StringIO()
    if stdin is not None:
        import sys
        old=sys.stdin
        sys.stdin=io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code=rd.main(argv)
    finally:
        if stdin is not None:
            sys.stdin=old
    return code,out.getvalue(),err.getvalue()


class Registry(unittest.TestCase):
    def setUp(self):
        self.raw=json.loads((DASH/'widgets.json').read_text(encoding='utf-8'))

    def test_top_level_is_array_with_unique_complete_entries(self):
        self.assertIsInstance(self.raw,list)
        self.assertGreaterEqual(len(self.raw),15)
        ids=[w['id'] for w in self.raw]
        self.assertEqual(len(set(ids)),len(ids),'id 有重复: %s'%ids)
        for w in self.raw:
            for f in REQUIRED_FIELDS:
                self.assertIn(f,w,'部件 %s 缺字段 %s'%(w.get('id'),f))
            self.assertIsInstance(w['default'],bool)
            self.assertIsInstance(w['minWidth'],int)
            self.assertIsInstance(w['minHeight'],int)
            self.assertGreater(w['minWidth'],0)
            self.assertGreater(w['minHeight'],0)
            self.assertTrue(re.match(r'^draw_[a-z0-9_]+$',w['draw']),'draw 名不符: %s'%w['draw'])
            self.assertTrue(w['label'] and w['desc'] and w['group'],'%s 的空文本字段'%w['id'])

    def test_required_ids_and_groups_present(self):
        ids={w['id'] for w in self.raw}
        for r in REQUIRED_IDS:self.assertIn(r,ids,'缺必需部件 %s'%r)
        groups={w['group'] for w in self.raw}
        for g in ['时间','性能','网络','存储','电源','系统']:
            self.assertIn(g,groups,'缺分组 %s'%g)

    def test_every_widget_is_implemented_in_render_py(self):
        code,out,err=main_args(['--list'])
        self.assertEqual(code,0)
        reg=json.loads(out)
        impl={w['id']:w for w in reg['widgets']}
        self.assertEqual(len(reg['widgets']),len(self.raw))
        for w in self.raw:
            self.assertIn(w['id'],impl,'render.py 未注册 %s'%w['id'])
            self.assertTrue(impl[w['id']]['implemented'],'%s 的 draw 函数未实现'%w['id'])
            self.assertEqual(impl[w['id']]['draw'],w['draw'])
        self.assertIn(rd.REGISTRY[0]['draw'],rd.DRAW_FNS)
        for w in self.raw:self.assertIn(w['draw'],rd.DRAW_FNS)

    def test_list_and_layout_cli_need_no_in_out(self):
        for flag,key in [('--list','widgets'),('--layout','boxes')]:
            code,out,err=main_args([flag])
            self.assertEqual(code,0,'%s 应不需要 --in/--out'%flag)
            self.assertIn(key,json.loads(out))

    def test_presets_exist_and_are_registered(self):
        for p in PRESET_NAMES:self.assertIn(p,rd.PRESETS)
        for name,ids in rd.PRESETS.items():
            for i in ids:
                self.assertIn(i,rd.WMAP,'预设 %s 引用了未注册 id %s'%(name,i))


class RenderOutput(unittest.TestCase):
    def _render(self,state=None,widgets=None,preset='default',quality=72,limit=None,history=None):
        return rd.render(dict(STATE) if state is None else state,widgets,preset,quality,limit,history)

    def test_synthetic_state_gives_baseline_480x360(self):
        data,img=self._render()
        marker,prec,w,h=sof(data)
        self.assertEqual((w,h),(W,H))
        self.assertEqual(prec,8)
        self.assertEqual(marker,0xC0,'SOF 标记 0x%02X 不是 baseline 0xC0'%marker)
        self.assertNotEqual(marker,0xC2,'progressive 不被设备支持')
        self.assertEqual(img.size,(W,H))

    def test_every_preset_gives_correct_size_and_baseline(self):
        for p in PRESET_NAMES:
            with self.subTest(preset=p):
                data,img=self._render(preset=p)
                marker,prec,w,h=sof(data)
                self.assertEqual((w,h),(W,H))
                self.assertEqual((marker,prec),(0xC0,8))
                self.assertEqual(img.size,(W,H))
                self.assertLess(len(data),30000,'预设 %s 超出 30000 B'%p)

    def test_every_widget_renders_alone(self):
        for w in REQUIRED_IDS:
            with self.subTest(widget=w):
                data,_=self._render(widgets=[w])
                marker,prec,wd,ht=sof(data)
                self.assertEqual((wd,ht,marker,prec),(W,H,0xC0,8))

    def test_unknown_widget_is_rejected_with_nonzero_exit(self):
        for bad in ['nosuch','cpu,nosuch','CPU']:
            with self.subTest(widgets=bad):
                with self.assertRaises(rd.UnknownWidget):rd.layout(['cpu',bad])
                with tempfile.TemporaryDirectory() as t:
                    out=Path(t)/'o.jpg'
                    code,_,err=main_args(['--in','-','--out',str(out),'--widgets',bad],json.dumps(STATE))
                    self.assertNotEqual(code,0,'未注册 id %r 应非 0 退出'%bad)
                    self.assertIn('未注册的部件 id',err)
                    self.assertFalse(out.exists(),'拒绝时不应写出文件')
        # 空串/纯空白不是合法 id；resolve 会剔除它们，剥完为空则退回预设
        with self.assertRaises(rd.UnknownWidget):rd.layout(['cpu',''])
        self.assertEqual(rd.resolve_widgets(['cpu,',''],'minimal'),['cpu,'])

    def test_case_sensitive_ids(self):
        self.assertRaises(rd.UnknownWidget,rd.layout,['CPU'])
        self.assertRaises(rd.UnknownWidget,rd.layout,['Clock_Big'])

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(ValueError):rd.resolve_widgets(None,'nosuch')
        with self.assertRaises(rd.UnknownWidget):rd.layout(['cpu','definitely_not_a_widget'])

    def test_missing_or_null_fields_do_not_crash(self):
        cases=[{}, {k:None for k in STATE}, {'cpu_load':3},
               {'clock':12345,'cpu_load':'abc','bat_charging':'yes','uptime':[],'gpu_pct':{},'mem_pct':float('nan')},
               {'host':'X'*500,'top_proc':'Y'*500,'ip':'1.2.3.4'*50},
               {'cpu_load':9999,'mem_pct':-50,'bat_pct':12345,'gpu_pct':10**9,'temp_c':-999}]
        for i,st in enumerate(cases):
            with self.subTest(case=i):
                data,img=self._render(state=st,preset='full')
                self.assertEqual(sof(data)[2:],(W,H))
                self.assertEqual(img.size,(W,H))

    def test_empty_and_nondict_state_via_cli(self):
        with tempfile.TemporaryDirectory() as t:
            for raw in ['{}','[]','null','{"cpu_load":null}']:
                out=Path(t)/'o.jpg'
                code,_,err=main_args(['--in','-','--out',str(out),'--preset','full'],raw)
                self.assertEqual(code,0,'state %s 应能正常出图: %s'%(raw,err))
                self.assertEqual(sof(out.read_bytes())[2:],(W,H))

    def test_bad_json_exits_2_without_partial_file(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)/'never.jpg'
            code,_,err=main_args(['--in','-','--out',str(out)],'{not json')
            self.assertEqual(code,2)
            self.assertIn('JSON 解析失败',err)
            self.assertFalse(out.exists())

    def test_limit_triggers_quality_bisect(self):
        big,_=self._render(preset='default',quality=72)
        self.assertGreater(len(big),12000)
        small,_=self._render(preset='default',quality=72,limit=12000)
        self.assertLessEqual(len(small),12000,'--limit 未生效: %d B'%len(small))
        self.assertEqual(sof(small)[2:],(W,H))
        # 降质量后应仍能被设备接受（baseline + 8 位）
        self.assertEqual(sof(small)[0],0xC0)

    def test_n_a_placeholder_drawn_for_missing_values(self):
        img=rd.render({},None,'minimal')[1]
        colors=img.getcolors(maxcolors=1<<24)
        self.assertTrue(colors,'空 state 渲染出全空图')
        self.assertGreater(len(colors),5,'空 state 的配色过于单薄，n/a 占位可能没画')

    def test_chinese_text_forces_cjk_capable_font(self):
        f_cjk=rd.F(20,'mono',text='中文')
        f_latin=rd.F(20,'mono',text='ABCDEF')
        self.assertIsNotNone(f_cjk)
        self.assertIsNotNone(f_latin)
        self.assertEqual(f_cjk.path,rd.FONT_REG,'含汉字时必须走 msyh，否则渲染成方块')


class Layout(unittest.TestCase):
    def test_layout_never_leaves_canvas(self):
        cases=[list(rd.PRESETS[p]) for p in PRESET_NAMES]
        cases.append(list(rd.WMAP))
        cases.append(['clock_big'])
        for ids in cases:
            with self.subTest(n=len(ids)):
                boxes,info=rd.layout(ids)
                self.assertFalse(info['overflow'],'布局越界: %s'%info)
                self.assertEqual(len(boxes),len(ids))
                self.assertEqual([b['id'] for b in boxes],ids)
                for b in boxes:
                    self.assertGreaterEqual(b['x'],0);self.assertGreaterEqual(b['y'],0)
                    self.assertLessEqual(b['x']+b['w'],W,'%s 右越界'%b['id'])
                    self.assertLessEqual(b['y']+b['h'],H,'%s 下越界'%b['id'])
                    self.assertGreater(b['w'],0);self.assertGreater(b['h'],0)

    def test_boxes_do_not_overlap(self):
        for ids in [list(rd.PRESETS[p]) for p in PRESET_NAMES]+[list(rd.WMAP)]:
            boxes,_=rd.layout(ids)
            for i in range(len(boxes)):
                for j in range(i+1,len(boxes)):
                    a,b=boxes[i],boxes[j]
                    overlap = (a['x']<b['x']+b['w'] and b['x']<a['x']+a['w']
                               and a['y']<b['y']+b['h'] and b['y']<a['y']+a['h'])
                    self.assertFalse(overlap,'%s 与 %s 重叠'%(a['id'],b['id']))

    def test_every_widget_alone_stays_inside(self):
        for wid in rd.WMAP:
            boxes,info=rd.layout([wid])
            self.assertFalse(info['overflow'])
            b=boxes[0]
            self.assertLessEqual(b['x']+b['w'],W)
            self.assertLessEqual(b['y']+b['h'],H)

    def test_widgets_option_wins_over_preset(self):
        self.assertEqual(rd.resolve_widgets(['cpu','mem'],'full'),['cpu','mem'])
        self.assertEqual(rd.resolve_widgets(None,'minimal'),list(rd.PRESETS['minimal']))
        self.assertEqual(rd.resolve_widgets(['cpu,','','  '],'full'),['cpu,'])


class History(unittest.TestCase):
    def test_append_creates_dir_and_trims_to_sixty(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'deep'/'state'
            self.assertFalse(d.exists())
            for i in range(65):
                hist=rd.append_history(str(d),{'clock':'%02d:00:00'%(i%24),'cpu_load':float(i),'mem_pct':50.0})
            self.assertTrue((d/'history.json').exists(),'state-dir 未自动创建')
            self.assertEqual(len(hist),60)
            self.assertEqual(hist[-1]['cpu'],64.0)
            self.assertEqual(hist[0]['cpu'],5.0)
            again=rd.load_history(str(d))
            self.assertEqual(again,hist)

    def test_broken_history_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'history.json').write_text('{not json',encoding='utf-8')
            self.assertEqual(rd.load_history(t),[])
            h=rd.append_history(t,{'clock':'01:00:00','cpu_load':1.0,'mem_pct':2.0})
            self.assertEqual(len(h),1)
            self.assertEqual(rd.load_history(None),[])
            self.assertEqual(rd.append_history(None,{}),[])

    def test_history_cpu_draws_with_history_from_state_dir(self):
        with tempfile.TemporaryDirectory() as t:
            for i in range(20):
                rd.append_history(t,{'clock':'%02d:00:00'%i,'cpu_load':float(10*i),'mem_pct':float(i)})
            hist=rd.load_history(t)
            self.assertEqual(len(hist),20)
            with tempfile.TemporaryDirectory() as o:
                out=Path(o)/'h.jpg'
                code,_,err=main_args(['--in','-','--out',str(out),'--widgets','history_cpu','--state-dir',t],json.dumps(STATE))
                self.assertEqual(code,0,err)
                self.assertEqual(sof(out.read_bytes())[2:],(W,H))
            data,_=rd.render(STATE,['history_cpu'],'default',72,None,hist)
            self.assertEqual(sof(data)[2:],(W,H))

    def test_history_cpu_survives_empty_history(self):
        for hist in [None,[],[{'t':None,'cpu':None,'mem':None}],[{'cpu_load':1}]]:
            with self.subTest(hist=hist):
                self.assertEqual(sof(rd.render(STATE,['history_cpu'],'default',72,None,hist)[0])[2:],(W,H))


class Cli(unittest.TestCase):
    def test_file_in_out_and_preview(self):
        with tempfile.TemporaryDirectory() as t:
            src=Path(t)/'state.json';src.write_text(json.dumps(STATE),encoding='utf-8')
            out=Path(t)/'sub'/'o.jpg';prev=Path(t)/'sub'/'o.png'
            code,stdout,err=main_args(['--in',str(src),'--out',str(out),'--preset','full',
                                       '--quality','72','--limit','30000','--preview',str(prev)])
            self.assertEqual(code,0,err)
            self.assertTrue(out.exists() and prev.exists())
            self.assertEqual(sof(out.read_bytes())[2:],(W,H))
            self.assertLessEqual(out.stat().st_size,30000)
            self.assertIn('480x360',stdout)

    def test_output_dir_is_created(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)/'a'/'b'/'c.jpg'
            code,_,err=main_args(['--in','-','--out',str(out)],json.dumps(STATE))
            self.assertEqual(code,0,err)
            self.assertTrue(out.exists())

    def test_missing_in_or_out_errors(self):
        for argv in [['--out','x.jpg'],['--in','-'],[]]:
            with self.subTest(argv=argv):
                out,err=io.StringIO(),io.StringIO()
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:rd.main(argv)
                self.assertNotEqual(cm.exception.code,0)


if __name__=='__main__':unittest.main()
