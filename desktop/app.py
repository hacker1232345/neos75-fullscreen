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

VERSION = '1.1.2'
PROJECT = Path(sys._MEIPASS) / 'project' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
NODE = Path(sys._MEIPASS) / ('runtime/node.exe' if sys.platform == 'win32' else 'runtime/node') if getattr(sys, 'frozen', False) else Path(shutil.which('node') or 'node')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
URL = 'https://github.com/hacker1232345/neos75-fullscreen'


def builder():
    spec = importlib.util.spec_from_file_location('fullscreen_builder', PROJECT / 'tools/build_fullscreen.py')
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
    return {'output': str(dst), 'bytes': len(result), 'sha256': m['target_sha256'], 'changed_bytes': m['changed_bytes'], 'force': force}


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
    validate_firmware(baseline, target, force=force)
    args = [mode, str(Path(target).resolve()), str(Path(baseline).resolve())]
    if yes:
        args.append('--yes')
    if force:
        args.append('--force')
    return node_run('tools/mcu_flash_live.js', args, baseline=baseline, on_line=on_line)


def self_test(on_line=None):
    checks = {}
    for label, script in [('mcu_transport', 'tools/test_mcu_transport.js'), ('backup', 'tools/test_mcu_backup.js'), ('wallpaper_protocol', 'wallpaper/selftest.js'), ('force_flash_model', 'tools/test_force_flash.js'),
                          ('native_modules', 'desktop/native_check.js')]:
        checks[label] = node_run(script, on_line=on_line)
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
            app = App()
            app.root.update()
            options = [b for b in app.buttons if isinstance(b, ttk.Checkbutton)]
            assert len(options) == 2 and not app.force.get()
            assert all(b.cget('text') == '无验证，直接打补丁' for b in options)
            options[0].invoke()
            app.root.update()
            assert app.force.get() and options[1].instate(['selected'])
            options[1].invoke()
            assert not app.force.get()
            app.root.destroy()
            data = {'result': 'PASS', 'gui': 'created and updated; force defaults off; both tabs share selection', 'device_write': False}
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
        home, media, maintenance = [ttk.Frame(book, padding=16) for _ in range(3)]
        book.add(home, text='设备与补丁')
        book.add(media, text='图片测试')
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
            self.run(title, lambda cb: generate(source, out, overwrite=True, force=force), lambda d: self.target.set(d['output']))

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
        if data['firmwareKind'] == 'supported-original':
            self.backup.set(data['file'])
            self.state.set('备份完整 · 原版固件已匹配，可生成全屏补丁')
        elif data['firmwareKind'] == 'already-fullscreen':
            self.state.set('备份完整 · 设备已是本项目全屏版，无需重复升级')
            messagebox.showinfo('设备已经全屏', '当前固件备份已保存。设备已是全屏版，无需再次生成或刷入补丁。', parent=self.root)
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
    App().root.mainloop()
