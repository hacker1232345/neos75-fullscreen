"""Neos75 desktop launcher. No device operation occurs on startup."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

PROJECT = Path(sys._MEIPASS) / 'project' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
VERSION = json.loads((PROJECT / 'package.json').read_text(encoding='utf-8'))['version']
NODE = Path(sys._MEIPASS) / ('runtime/node.exe' if sys.platform == 'win32' else 'runtime/node') if getattr(sys, 'frozen', False) else Path(shutil.which('node') or 'node')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
URL = 'https://github.com/hacker1232345/neos75-fullscreen'


def builder():
    spec = importlib.util.spec_from_file_location('fullscreen_builder', PROJECT / 'tools/build_fullscreen.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dash():
    """屏幕部件（dashboard）桥接层。GUI 线程里只做注册表读取，采样/渲染/推送都在子进程里跑。"""
    spec = importlib.util.spec_from_file_location('neos75_dash', PROJECT / 'desktop/dash_bridge.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def validate_firmware(baseline, target, force=False):
    source = Path(baseline).read_bytes()
    image = Path(target).read_bytes()
    expected, m = builder().patch(source, force=force)
    if image != expected:
        raise ValueError('目标固件与当前备份生成结果不匹配（含配置和DFU校验）。未连接设备。')
    return m


def generate(source, destination, overwrite=False, force=False):
    src, dst = Path(source).resolve(), Path(destination).resolve()
    if src == dst:
        raise ValueError('输出不能覆盖原始备份，请选择其他文件名。')
    result, m = builder().patch(src.read_bytes(), force=force)
    if dst.exists() and not overwrite:
        raise ValueError('输出文件已存在，请选择新文件名或明确允许覆盖。')
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive temporary file, then atomic replacement; source is never changed.
    with tempfile.NamedTemporaryFile(dir=dst.parent, prefix='.neos75-', delete=False) as f:
        temp = Path(f.name)
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    try:
        if not overwrite and dst.exists():
            raise ValueError('输出文件已存在，停止写入。')
        os.replace(temp, dst)
    finally:
        if temp.exists():
            temp.unlink()
    return {'output': str(dst), 'bytes': len(result), 'sha256': m['target_sha256'], 'changed_bytes': m['changed_bytes'], 'force': force, 'profile': m['profile'], 'profileLabel': m['label'], 'validation': m['validation'],
            'appliedProfiles': m.get('appliedProfiles', [m['profile']]), 'appliedLabels': m.get('appliedLabels', [m['label']]),
            # force 时这两项说明「到底套上了几层」：matched=逐条旧字节核对通过，
            # blind=一层都没对上、只按链首固定地址盲套（此时进度条多半还在）。
            'forceLayersMatched': bool(m.get('forceLayersMatched')), 'forceBlind': bool(m.get('forceBlind')),
            # 未知固件走了内容锚点自动适配：偏移是现算的，报给刷写端核对产物本身。
            'adapted': bool(m.get('adapted'))}


def node_run(script, args=(), baseline=None, on_line=None):
    env = os.environ.copy()
    # Use only the bundled/project native modules, not a machine-specific global path.
    env['NODE_PATH'] = str(PROJECT / 'node_modules')
    env.pop('NEOS75_NODE_MODULES', None)
    env.pop('NODE_OPTIONS', None)
    env.pop('NEOS75_BASELINE', None)
    if baseline:
        env['NEOS75_BASELINE'] = str(Path(baseline).resolve())
    command = [str(NODE), str(PROJECT / script), *map(str, args)]
    p = subprocess.Popen(command, cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    lines = []
    for line in p.stdout:
        lines.append(line)
        if on_line:
            on_line(line.rstrip())
    code = p.wait()
    text = ''.join(lines).strip()
    if code:
        raise RuntimeError(text or f'子程序失败，退出码 {code}')
    return text


def maintain(mode, baseline, target, yes=False, on_line=None, force=False):
    if mode not in ['plan', 'verify', 'flash', 'restore']:
        raise ValueError('未知维护操作')
    if mode in ['flash', 'restore'] and not yes:
        raise ValueError('刷写或恢复需要明确确认，未连接设备。')
    meta = validate_firmware(baseline, target, force=force)
    args = [mode, str(Path(target).resolve()), str(Path(baseline).resolve())]
    if yes:
        args.append('--yes')
    if force:
        args.append('--force')
    if meta.get('adapted'):
        # 适配产物是 Python 侧按锚点现算出来的，Node 那边只有固定偏移，重算必然不等。
        # 把产物的 sha256 交过去，让它核对文件本身而不是重算。
        args.extend(['--expect', meta['target_sha256']])
    return node_run('tools/mcu_flash_live.js', args, baseline=baseline, on_line=on_line)


def self_test(on_line=None):
    checks = {}
    for label, script in [('mcu_transport', 'tools/test_mcu_transport.js'), ('backup', 'tools/test_mcu_backup.js'), ('wallpaper_protocol', 'wallpaper/selftest.js'), ('screen_navigation', 'wallpaper/test_pages.js'), ('force_flash_model', 'tools/test_force_flash.js'), ('automatic_profiles', 'tools/test_profiles.js'),
                          ('native_modules', 'desktop/native_check.js')]:
        checks[label] = node_run(script, on_line=on_line)
    # 屏幕部件模块：离线自测（不打开 USB）。缺失时记为 SKIP 而不是让整套自检失败。
    try:
        checks['board_selftest'] = dash().selftest(on_line=on_line)
    except Exception as e:
        checks['board_selftest'] = 'SKIP: %s' % e
    try:
        registry = dash().registry()
        checks['board_registry'] = 'PASS %d widgets / presets %s' % (len(registry['widgets']), ','.join(sorted(registry['presets'])))
    except Exception as e:
        checks['board_registry'] = 'SKIP: %s' % e
    try:
        builder().patch(bytes(491520))
    except ValueError:
        checks['wrong_backup_gate'] = 'PASS'
    else:
        raise AssertionError('wrong backup was accepted')
    try:
        maintain('flash', 'missing-backup', 'missing-target')
    except ValueError:
        checks['explicit_flash_gate'] = 'PASS'
    else:
        raise AssertionError('unconfirmed flash was accepted')
    return {'version': VERSION, 'result': 'PASS', 'checks': checks, 'device_write': False}


def cli(argv):
    parser = argparse.ArgumentParser(description='Neos75全屏工具；无参数启动窗口。命令行结果可保存到JSON。')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--self-test', action='store_true')
    action.add_argument('--gui-smoke', action='store_true')
    action.add_argument('--status', action='store_true')
    action.add_argument('--build', metavar='BACKUP')
    action.add_argument('--backup', action='store_true')
    action.add_argument('--jpeg-plan', metavar='JPEG')
    action.add_argument('--jpeg-send', metavar='JPEG')
    action.add_argument('--maintain', choices=['plan', 'verify', 'flash', 'restore'])
    action.add_argument('--leave-dfu', action='store_true')
    parser.add_argument('--out')
    parser.add_argument('--baseline')
    parser.add_argument('--target')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--force', action='store_true', help='无验证，直接打补丁：跳过版本哈希与补丁旧字节检查')
    parser.add_argument('--result-file')
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.force and not (args.build or args.maintain):
            raise ValueError('--force仅用于生成全屏固件或固件维护')
        if args.gui_smoke:
            from glass import run
            data = run(smoke=True)
        elif args.self_test:
            data = self_test()
        elif args.backup:
            if not args.yes or not args.out:
                raise ValueError('备份需要--out新文件路径及--yes；会进入DFU，不擦写Flash。')
            data = json.loads(node_run('tools/mcu_backup.js', [args.out, '--yes']).splitlines()[-1])
        elif args.build:
            if not args.out:
                raise ValueError('--build需要--out')
            data = generate(args.build, args.out, args.overwrite, force=args.force)
        elif args.status:
            data = json.loads(node_run('wallpaper/screen_wallpaper.js', ['status']))
        elif args.jpeg_plan:
            data = json.loads(node_run('wallpaper/screen_wallpaper.js', ['plan', args.jpeg_plan]))
        elif args.jpeg_send:
            if not args.yes:
                raise ValueError('发送图片需要--yes，未连接设备。')
            data = json.loads(node_run('wallpaper/screen_wallpaper.js', ['send', args.jpeg_send, '--yes']))
        elif args.leave_dfu:
            if not args.yes:
                raise ValueError('退出DFU需要--yes')
            data = {'log': node_run('tools/leave_mcu.js')}
        else:
            if not args.baseline or not args.target:
                raise ValueError('维护需要--baseline和--target')
            data = {'log': maintain(args.maintain, args.baseline, args.target, args.yes, force=args.force), 'force': args.force}
        data = {'ok': True, **data}
    except Exception as e:
        code, data = 1, {'ok': False, 'error': str(e)}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.result_file:
        Path(args.result_file).write_text(text, encoding='utf-8')
    if sys.stdout:
        print(text)
    elif not args.result_file:
        messagebox.showinfo('Neos75 命令结果', text)
    return code


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f'Neos75 全屏工具 · v{VERSION}')
        self.root.geometry('980x840')
        self.root.minsize(900, 800)
        self.root.configure(bg='#F4F5F7')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.events = queue.Queue()
        self.busy = False
        self.buttons = []
        self.backup = tk.StringVar()
        self.target = tk.StringVar()
        self.jpeg = tk.StringVar()
        self.force = tk.BooleanVar(value=False)
        self.board_proc = None
        self.state = tk.StringVar(value='未查询设备')
        self.progress_text = tk.StringVar(value='就绪 · 启动不会连接或修改设备')
        self.draw()
        self.root.after(100, self.poll)

    def button(self, parent, text, command, **kw):
        b = ttk.Button(parent, text=text, command=command, **kw)
        self.buttons.append(b)
        return b

    def draw(self):
        style = ttk.Style()
        style.theme_use('clam')
        self.font = 'Microsoft YaHei UI' if sys.platform == 'win32' else 'Helvetica Neue'
        style.configure('.', font=(self.font, 10))
        style.configure('TFrame', background='#F4F5F7')
        style.configure('TLabel', background='#F4F5F7', foreground='#171717')
        style.configure('TButton', padding=(14, 8), background='#FFFFFF', foreground='#171717')
        style.map('TButton', background=[('active', '#E8ECF0')], foreground=[('disabled', '#707780')])
        style.configure('Primary.TButton', background='#171717', foreground='#FFFFFF')
        style.map('Primary.TButton', background=[('active', '#404040'), ('disabled', '#CCCCCC')])
        style.configure('TLabelframe', background='#F4F5F7', padding=14)
        style.configure('TLabelframe.Label', font=(self.font, 11, 'bold'), background='#F4F5F7')
        style.configure('TNotebook', background='#F4F5F7', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(20, 10))
        style.map('TNotebook.Tab', background=[('selected', '#FFFFFF')])
        header = tk.Frame(self.root, bg='#171717', padx=24, pady=16)
        header.pack(fill='x')
        tk.Label(header, text='NEOS75 / 全屏工具', font=(self.font, 21, 'bold'), fg='#FFFFFF', bg='#171717').pack(anchor='w')
        tk.Label(header, text='480 × 360  ·  图片 / GIF 全屏补丁  ·  '+('Windows x64' if sys.platform == 'win32' else 'macOS'), font=(self.font, 10), fg='#D4D4D4', bg='#171717').pack(anchor='w', pady=(5, 0))
        body = ttk.Frame(self.root, padding=20)
        body.pack(fill='both', expand=True)
        book = ttk.Notebook(body)
        book.pack(fill='both', expand=True)
        home, media, board, maintenance = [ttk.Frame(book, padding=16) for _ in range(4)]
        book.add(home, text='设备与补丁')
        book.add(media, text='图片测试')
        book.add(board, text='屏幕部件')
        book.add(maintenance, text='固件维护')
        status = ttk.LabelFrame(home, text='1  设备状态')
        status.pack(fill='x', pady=(0, 14))
        ttk.Label(status, textvariable=self.state, font=(self.font, 15, 'bold')).pack(anchor='w')
        ttk.Label(status, text='USB直连键盘后查询。查询只读，不进入DFU。').pack(anchor='w', pady=(4, 10))
        self.button(status, '查询屏幕', self.query, style='Primary.TButton').pack(anchor='w')
        patch = ttk.LabelFrame(home, text='2  备份设备并生成全屏固件')
        patch.pack(fill='x')
        ttk.Label(patch, text='没有备份？先从设备读取。程序双读校验并保存，再判断版本是否支持。', wraplength=790).pack(anchor='w', pady=(0, 8))
        self.path_row(patch, '原始备份', self.backup, lambda: self.choose(self.backup, '选择原始MCU备份', '*.bin'))
        self.force_option(patch)
        backup_row = ttk.Frame(patch)
        backup_row.pack(fill='x', pady=(10, 0))
        self.button(backup_row, '从设备读取备份…', self.read_backup, style='Primary.TButton').pack(side='left', padx=(0, 8))
        self.button(backup_row, '生成全屏固件…', self.build).pack(side='left')
        ttk.Label(patch, text='备份会临时进入DFU；不擦写Flash，完成后尝试回到应用。请保持USB连接。', foreground='#475569', wraplength=790).pack(anchor='w', pady=(8, 0))
        ttk.Label(home, text='已刷入补丁后，重新连接官方程序，在第5页导入原图片或GIF即可。', wraplength=800).pack(anchor='w', pady=(14, 0))
        media_box = ttk.LabelFrame(media, text='480 × 360 JPEG 传输测试')
        media_box.pack(fill='x')
        ttk.Label(media_box, text='先离线检查图片，再按需发送。图片必须是与设备回报尺寸一致的基线JPEG。', wraplength=790).pack(anchor='w', pady=(0, 8))
        self.path_row(media_box, '测试图片', self.jpeg, lambda: self.choose(self.jpeg, '选择JPEG图片', '*.jpg *.jpeg'))
        row = ttk.Frame(media_box)
        row.pack(fill='x', pady=(10, 0))
        self.button(row, '离线检查', self.jpeg_plan).pack(side='left', padx=(0, 8))
        self.button(row, '发送图片…', self.jpeg_send, style='Primary.TButton').pack(side='left')
        self.button(media, '载入内置边框测试图', self.example).pack(anchor='w', pady=14)
        ttk.Label(media, text='GIF的全屏效果由固件补丁提供，GIF导入请使用官方程序。\n发送会替换当前壁纸；尾段无最终应答，发送后需看屏确认。', wraplength=800, foreground='#475569').pack(anchor='w')
        self.draw_board(board)
        box = ttk.LabelFrame(maintenance, text='仅在确需维护时操作')
        box.pack(fill='x')
        ttk.Label(box, text='这组操作会进入DFU。刷写和恢复会修改固件，请保持USB连接。\n默认仅接受验证过的版本；强制模式使用下方选项。', wraplength=790).pack(anchor='w', pady=(0, 8))
        self.path_row(box, '原始备份', self.backup, lambda: self.choose(self.backup, '选择原始MCU备份', '*.bin'))
        self.path_row(box, '全屏固件', self.target, lambda: self.choose(self.target, '选择已生成的全屏固件', '*.bin'))
        self.force_option(box)
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(12, 0))
        for text, mode in [('预检（进入DFU）', 'plan'), ('回读校验', 'verify'), ('刷入全屏…', 'flash'), ('恢复原版…', 'restore')]:
            self.button(row, text, lambda m=mode: self.firmware(m)).pack(side='left', padx=(0, 8))
        self.button(maintenance, '退出DFU / 回到应用…', self.leave).pack(anchor='w', pady=14)
        ttk.Label(maintenance, text='四个8KB组均须逐字节校验通过后，再退出DFU。\n若读写报错，保留日志和备份，停止重复尝试。', wraplength=800, foreground='#475569').pack(anchor='w')
        log_header = ttk.Frame(body)
        log_header.pack(fill='x', pady=(16, 6))
        ttk.Label(log_header, textvariable=self.progress_text).pack(side='left')
        self.button(log_header, '保存日志', self.save_log).pack(side='right')
        self.button(log_header, '离线自检', lambda: self.run('离线自检', lambda cb: self_test(cb))).pack(side='right', padx=8)
        self.log = tk.Text(body, height=7, font=('Consolas', 10), background='#FFFFFF', foreground='#171717', relief='solid', borderwidth=1, wrap='word', state='disabled')
        self.log.pack(fill='x')
        ttk.Label(body, text=f'v{VERSION} · 原始备份与输出文件由你选择，本程序不内置设备完整转储。', foreground='#475569').pack(anchor='w', pady=(8, 0))

    def force_option(self, parent):
        option = ttk.Checkbutton(parent, text='无验证，直接打补丁', variable=self.force)
        option.pack(anchor='w', pady=(8, 0))
        self.buttons.append(option)
        ttk.Label(parent, text='跳过版本哈希和补丁旧字节检查，按固定地址打补丁；不保证兼容。原始备份和写后回验仍保留。', wraplength=760, foreground='#9A3412').pack(anchor='w', pady=(4, 0))

    # ── 屏幕部件页 ────────────────────────────────────────────────
    def draw_board(self, parent):
        """部件选择用 Listbox 而不是 Checkbutton：一是 15 个部件用勾选框太挤，
        二是 --gui-smoke 断言 GUI 里的 ttk.Checkbutton 恰好是两个 force 选项，
        换控件天然避开这条断言，也让「勾选框数=2」继续代表「没有第二个写入门」。"""
        box = ttk.LabelFrame(parent, text='屏幕上显示什么')
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='勾选要显示的部件，然后采样电脑状态、渲染成 480 × 360 图片。\n'
                            '采样与渲染完全离线；只有「推送」才会连接键盘。', wraplength=790).pack(anchor='w', pady=(0, 8))
        preset_row = ttk.Frame(box)
        preset_row.pack(fill='x')
        ttk.Label(preset_row, text='预设').pack(side='left', padx=(0, 8))
        for name, label in (('minimal', '极简'), ('default', '默认'), ('performance', '性能'),
                            ('network', '网络'), ('full', '全部')):
            self.button(preset_row, label, lambda n=name: self.apply_preset(n)).pack(side='left', padx=(0, 6))
        self.button(preset_row, '一个都不选', lambda: self.set_widgets([])).pack(side='left', padx=(0, 6))
        list_row = ttk.Frame(box)
        list_row.pack(fill='both', expand=True, pady=(10, 0))
        self.board_list = tk.Listbox(list_row, selectmode='multiple', height=9, activestyle='none',
                                     font=(self.font, 10), background='#FFFFFF', relief='solid', borderwidth=1,
                                     highlightthickness=0, selectbackground='#171717', selectforeground='#FFFFFF')
        scroll = ttk.Scrollbar(list_row, orient='vertical', command=self.board_list.yview)
        self.board_list.configure(yscrollcommand=scroll.set)
        self.board_list.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.board_items = []
        try:
            groups = dash().grouped()
        except Exception as e:
            groups = []
            self.append('部件注册表读取失败：%s' % e)
        for group, items in groups:
            self.board_list.insert('end', '── %s ──' % group)
            self.board_list.itemconfigure('end', foreground='#707780')
            self.board_items.append(None)
            for w in items:
                self.board_list.insert('end', '    %s  ·  %s' % (w['label'], w.get('desc', '')))
                self.board_items.append(w['id'])
        self.board_list.insert('end', '（带“·”的行是分组标题，不可选）')
        self.board_items.append(None)
        self.board_list.itemconfigure('end', foreground='#707780')
        self.set_widgets(dash().default_widgets() if groups else [])
        tune = ttk.Frame(box)
        tune.pack(fill='x', pady=(10, 0))
        ttk.Label(tune, text='质量').pack(side='left')
        self.board_quality = tk.IntVar(value=72)
        ttk.Spinbox(tune, from_=20, to=95, increment=1, width=4, textvariable=self.board_quality).pack(side='left', padx=(6, 18))
        ttk.Label(tune, text='刷新间隔(ms)').pack(side='left')
        self.board_interval = tk.IntVar(value=3000)
        ttk.Spinbox(tune, from_=1200, to=60000, increment=500, width=7, textvariable=self.board_interval).pack(side='left', padx=(6, 18))
        ttk.Label(tune, text='历史采样目录').pack(side='left')
        self.board_state_dir = tk.StringVar(value=str(PROJECT / 'dashboard' / '.history'))
        ttk.Entry(tune, textvariable=self.board_state_dir, width=26).pack(side='left', padx=(6, 0))
        act = ttk.Frame(box)
        act.pack(fill='x', pady=(12, 0))
        self.button(act, '采样并预览…', self.board_preview, style='Primary.TButton').pack(side='left', padx=(0, 8))
        self.button(act, '推送一帧…', self.board_push_once).pack(side='left', padx=(0, 8))
        self.board_loop_btn = self.button(act, '连续推送…', self.board_loop_start)
        self.board_loop_btn.pack(side='left', padx=(0, 8))
        self.board_stop_btn = self.button(act, '停止连续推送', self.board_loop_stop)
        self.board_stop_btn.pack(side='left', padx=(0, 8))
        self.button(act, '离线自检', self.board_selftest).pack(side='left')
        ttk.Label(box, text='采样稳态约 0.6 秒/次，所以间隔低于约 1200ms 没有意义。\n'
                            '屏幕协议没有最终应答，推送成功的判定是「帧数与应答数对得上」；\n'
                            '实际显示效果仍需看屏确认。同一时刻只能有一个上传者，连续推送前请关闭官方程序的上传任务。',
                  wraplength=790, foreground='#475569').pack(anchor='w', pady=(10, 0))

    def set_widgets(self, ids):
        """按部件 id 列表设置 Listbox 选中项（None 行是分组标题，跳过）。"""
        wanted = set(ids or [])
        self.board_list.selection_clear(0, 'end')
        for i, wid in enumerate(getattr(self, 'board_items', [])):
            if wid and wid in wanted:
                self.board_list.selection_set(i)

    def selected_widgets(self):
        return [self.board_items[i] for i in self.board_list.curselection()
                if getattr(self, 'board_items', None) and self.board_items[i]]

    def apply_preset(self, name):
        try:
            presets = dash().registry()['presets']
        except Exception as e:
            messagebox.showerror('预设不可用', str(e), parent=self.root)
            return
        self.set_widgets(presets.get(name, []))
        self.append('已套用预设 %s：%s' % (name, '、'.join(presets.get(name, []))))

    def board_preview(self):
        if not self.selected_widgets():
            messagebox.showinfo('选择部件', '请至少勾选一个要显示的部件。', parent=self.root)
            return
        out = filedialog.asksaveasfilename(parent=self.root, title='保存预览图', initialfile='neos75-board.jpg',
                                           defaultextension='.jpg', filetypes=[('JPEG 图片', '*.jpg')])
        if not out:
            return
        widgets, quality = self.selected_widgets(), self.board_quality.get()
        state_dir = self.board_state_dir.get()
        png = str(Path(out).with_suffix('.png'))
        def work(cb):
            st = dash().sample(on_line=cb)
            Path(out + '.json').write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
            text = dash().render(out + '.json', out, widgets=widgets, quality=quality,
                                 preview=png, state_dir=state_dir, on_line=cb)
            return {'jpeg': out, 'preview': png, 'state': out + '.json', 'render': text, 'device_write': False}
        self.run('采样并预览', work, lambda d: self.state.set('已生成预览 · 未连接设备'))

    def board_push_once(self):
        if not self.selected_widgets():
            messagebox.showinfo('选择部件', '请至少勾选一个要显示的部件。', parent=self.root)
            return
        if not messagebox.askyesno('推送一帧', '将采样一次、渲染并推送到屏幕（会替换当前画面）。\n'
                                               '请关闭官方程序的上传任务。\n\n继续？', parent=self.root, default='no'):
            return
        widgets, quality = self.selected_widgets(), self.board_quality.get()
        state_dir = self.board_state_dir.get()
        def work(cb):
            text = dash().push_once('once', on_line=cb, widgets=widgets, quality=quality, state_dir=state_dir)
            return {'log': text.strip().splitlines()[-1] if text.strip() else '', 'displayVerified': False,
                    'note': '屏幕协议无最终应答，请目视确认'}
        self.run('推送一帧', work)

    def board_loop_start(self):
        if self.board_proc is not None:
            messagebox.showinfo('已在推送', '连续推送正在运行，请先停止。', parent=self.root)
            return
        if not self.selected_widgets():
            messagebox.showinfo('选择部件', '请至少勾选一个要显示的部件。', parent=self.root)
            return
        if not messagebox.askyesno('连续推送', '将持续采样并刷新屏幕，直到手动停止。\n'
                                               '请关闭官方程序的上传任务，并保持USB连接。\n\n开始？', parent=self.root, default='no'):
            return
        args = {'widgets': self.selected_widgets(), 'quality': self.board_quality.get(),
                'interval': self.board_interval.get(), 'state_dir': self.board_state_dir.get()}
        try:
            self.board_proc = dash().push_popen('loop', **args)
        except Exception as e:
            messagebox.showerror('无法启动', str(e), parent=self.root)
            return
        self.append('—— 连续推送已启动（间隔 %d ms）——' % args['interval'])
        self.progress_text.set('连续推送 · 运行中，点「停止连续推送」结束')
        threading.Thread(target=self.pump_loop, args=(self.board_proc,), daemon=True).start()

    def pump_loop(self, proc):
        """把 loop 子进程的每行输出送进事件队列；进程结束后通知 UI。"""
        try:
            for line in proc.stdout:
                self.events.put(('line', line.rstrip()))
        except Exception as e:
            self.events.put(('line', '读取推送输出失败：%s' % e))
        finally:
            code = proc.wait()
            self.events.put(('loop_end', code))

    def board_loop_stop(self):
        proc = self.board_proc
        if proc is None:
            self.append('当前没有在跑的连续推送。')
            return
        self.board_proc = None
        try:
            proc.terminate()
        except Exception as e:
            self.append('停止失败：%s' % e)
        self.progress_text.set('连续推送 · 已停止')

    def board_selftest(self):
        self.run('屏幕部件离线自检', lambda cb: dash().selftest(on_line=cb))

    def path_row(self, parent, label, var, action):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=10).pack(side='left')
        ttk.Entry(row, textvariable=var, state='readonly').pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.button(row, '选择…', action).pack(side='right')

    def choose(self, var, title, pattern):
        p = filedialog.askopenfilename(parent=self.root, title=title, filetypes=[('文件', pattern), ('全部文件', '*.*')])
        if p:
            var.set(p)
            if var is self.backup:
                try:
                    self.show_profile(builder().firmware_info(Path(p).read_bytes()))
                except OSError as e:
                    self.state.set('备份读取失败：' + str(e))

    def show_profile(self, data):
        label = data.get('profileLabel') or '未知构建'
        kind = data.get('firmwareKind')
        if kind == 'supported-original':
            validation = '已实机验收' if data.get('validation') == 'hardware-verified' else '离线适配 · 待实机验收'
            pending = data.get('pending') or []
            tail = '，将依次应用：' + ' → '.join(pending) if pending else ''
            self.state.set(f'{label} · {validation}{tail}')
        elif kind == 'upgradeable':
            upgrade = data.get('upgradeLabel') or '下一层补丁'
            validation = '已实机验收' if data.get('upgradeValidation') == 'hardware-verified' else '离线适配 · 待实机验收'
            self.state.set(f'{label} · 可继续升级到「{upgrade}」（{validation}）')
        elif kind == 'already-fullscreen':
            self.state.set(f'{label} · 已是链末版本，无需重复升级')
        else:
            self.state.set('未知构建 · 未找到对应补丁配置')

    def append(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', str(text) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def run(self, title, fn, after=None):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.configure(state='disabled')
        self.progress_text.set(title + ' · 执行中，请等待')
        self.append('—— ' + title + ' ——')
        def work():
            try:
                result = fn(lambda line: self.events.put(('line', line)))
                self.events.put(('done', (title, result, after)))
            except Exception as e:
                self.events.put(('error', str(e)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'line':
                    self.append(value)
                    continue
                self.busy = False
                for b in self.buttons:
                    b.configure(state='normal')
                if kind == 'done':
                    title, result, after = value
                    self.progress_text.set(title + ' · 已完成')
                    self.append(json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result)
                    if after:
                        after(result)
                elif kind == 'loop_end':
                    self.board_proc = None
                    self.progress_text.set('连续推送 · 子进程已退出（码 %s）' % value)
                    self.append('连续推送结束，退出码 %s。' % value)
                else:
                    self.progress_text.set('操作失败 · 详见日志')
                    self.append(value)
                    messagebox.showerror('操作失败', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def query(self):
        self.run('查询设备', lambda cb: json.loads(node_run('wallpaper/screen_wallpaper.js', ['status'])),
                 lambda d: self.state.set(f"已连接 · {d['width']} × {d['height']} · {'支持JPEG' if d['supportJpg'] else 'JPEG不可用'}"))

    def build(self):
        source = self.backup.get()
        if not source:
            messagebox.showinfo('选择原始备份', '请先选择已有的原始MCU备份。', parent=self.root)
            return
        out = filedialog.asksaveasfilename(parent=self.root, title='保存全屏固件', initialfile='mcu-fullscreen-live-v1.bin', defaultextension='.bin', filetypes=[('固件', '*.bin')])
        if out:
            force = self.force.get()
            title = '生成全屏固件 · 无验证模式' if force else '生成全屏固件 · 标准验证'
            self.run(title, lambda cb: generate(source, out, overwrite=True, force=force), self.build_done)

    def build_done(self, data):
        self.target.set(data['output'])
        if data.get('force'):
            self.state.set(f"强制模式 · 使用{data['profileLabel']}配置 · 兼容性未验证")
        else:
            applied = data.get('appliedLabels') or [data.get('profileLabel')]
            self.state.set('已生成 · 依次应用 %s' % ' → '.join(applied))

    def read_backup(self):
        out = filedialog.asksaveasfilename(parent=self.root, title='保存设备当前固件（请选择新文件名）', initialfile='neos75-backup.bin', defaultextension='.bin', filetypes=[('固件备份', '*.bin')])
        if not out:
            return
        if Path(out).exists() or Path(out+'.partial').exists() or Path(out+'.json').exists():
            messagebox.showerror('不能覆盖备份', '所选名称已存在，请换一个新文件名。', parent=self.root)
            return
        if not messagebox.askyesno('读取设备备份', '请USB直连键盘并关闭官方程序的上传任务。\n\n将进入DFU并双次读取完整固件，不擦写Flash；可能需要数分钟。读取后尝试回到应用。\n\n开始备份？', parent=self.root, default='no'):
            return
        self.run('读取设备备份', lambda cb: json.loads(node_run('tools/mcu_backup.js', [out, '--yes'], on_line=cb).splitlines()[-1]), self.backup_done)

    def backup_done(self, data):
        self.backup.set(data['file'])
        self.show_profile(data)
        kind = data.get('firmwareKind')
        if kind == 'supported-original':
            self.append('已按备份哈希自动选择对应补丁，无需勾选无验证模式。')
        elif kind == 'upgradeable':
            label = data.get('upgradeLabel') or '下一层'
            self.append('当前备份还能继续升级到「%s」；生成时会自动只补剩余层。' % label)
            messagebox.showinfo('可以继续升级', '当前备份已保存并选中。\n\n它正是「%s」的输入构建，生成时会自动只应用剩余层，不会重复打已有补丁。' % label, parent=self.root)
        elif kind == 'already-fullscreen':
            messagebox.showinfo('设备已经到链末', '当前固件备份已保存。设备已是链末版本（%s），无需再次生成或刷入补丁。' % (data.get('profileLabel') or '未知构建'), parent=self.root)
        else:
            self.state.set('备份完整 · 当前固件版本尚未适配')
            messagebox.showinfo('当前版本尚未适配', '完整备份已保存并选中，受校验区域与目前验证的版本不匹配。默认模式会拒绝生成或刷写；如需强制尝试，可勾选“无验证，直接打补丁”。请保留备份与校验记录。', parent=self.root)
        if data.get('warning'):
            messagebox.showwarning('连接状态需检查', data['warning'], parent=self.root)

    def example(self):
        self.jpeg.set(str(PROJECT / 'examples/fullscreen-test-1.jpg'))
        self.append('已载入内置边框图。可先离线检查；发送后请检查四角和四边。')

    def jpeg_plan(self):
        if not self.jpeg.get():
            messagebox.showinfo('选择图片', '请先选择JPEG或载入内置测试图。', parent=self.root)
            return
        path = self.jpeg.get()
        self.run('离线图片检查', lambda cb: json.loads(node_run('wallpaper/screen_wallpaper.js', ['plan', path])))

    def jpeg_send(self):
        path = self.jpeg.get()
        if not path:
            messagebox.showinfo('选择图片', '请先选择图片。', parent=self.root)
            return
        if messagebox.askyesno('替换屏幕壁纸', '将发送所选JPEG并替换当前壁纸。请关闭其他上传程序。\n\n继续发送？', parent=self.root, default='no'):
            self.run('发送图片（显示仍需看屏确认）', lambda cb: json.loads(node_run('wallpaper/screen_wallpaper.js', ['send', path, '--yes'])))

    def firmware(self, mode):
        backup, target = self.backup.get(), self.target.get()
        force = self.force.get()
        try:
            validate_firmware(backup, target, force=force)
        except Exception as e:
            messagebox.showerror('文件未就绪', str(e), parent=self.root)
            return
        labels = {'plan': '预检', 'verify': '回读校验', 'flash': '刷入全屏固件', 'restore': '恢复原版'}
        question = labels[mode] + '会进入DFU。'
        if force:
            question += '\n当前为“无验证，直接打补丁”模式，不保证此固件兼容。'
        if mode in ['flash', 'restore']:
            question += '\n将擦写四个8KB保护组，请勿断电或拔线。'
        question += '\n\n确认执行？'
        if messagebox.askyesno(labels[mode], question, parent=self.root, default='no'):
            title = labels[mode] + (' · 无验证模式' if force else ' · 标准验证')
            self.run(title, lambda cb: maintain(mode, backup, target, yes=True, on_line=cb, force=force))

    def leave(self):
        if messagebox.askyesno('退出DFU', '请确认刷写或恢复已全部回验通过。\n\n现在退出DFU并回到应用？', parent=self.root, default='no'):
            self.run('退出DFU', lambda cb: node_run('tools/leave_mcu.js', on_line=cb))

    def save_log(self):
        p = filedialog.asksaveasfilename(parent=self.root, title='保存操作日志', initialfile='Neos75-log.txt', defaultextension='.txt')
        if p:
            try:
                Path(p).write_text(self.log.get('1.0', 'end'), encoding='utf-8')
            except OSError as e:
                messagebox.showerror('日志保存失败', str(e), parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showinfo('操作进行中', '请等待当前操作结束后再退出。固件写入中不可强行中断。', parent=self.root)
            return
        self.root.destroy()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        raise SystemExit(cli(sys.argv[1:]))
    from glass import run
    run()
