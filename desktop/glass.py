"""Local-only webview shell. No device writes occur until an explicit UI action.

The bridge accepts choices, never commands, filenames or arbitrary URLs. All
device tasks share one lock; firmware jobs cannot be cancelled mid-write.
"""
import base64
import copy
import datetime as dt
import importlib.util
import json
import os
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile
import time
import uuid

import psutil
from PIL import Image  # collected into the executable for the renderer module
import app as core


def user_directory():
    if sys.platform == 'win32':
        root = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library/Application Support'
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    return root / 'Neos75'


def load_renderer():
    spec = importlib.util.spec_from_file_location('neos75_renderer', core.PROJECT / 'dashboard/render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


class Sampler:
    """Portable baseline; unavailable sensors stay None, never synthetic values."""
    def __init__(self):
        self.net = psutil.net_io_counters()
        self.tick = time.monotonic()
        psutil.cpu_percent()
        self._extra = {}
        self._extra_lock = threading.Lock()
        self._collecting = False
        self._closed = False
        self._process = None

    def _enrich(self):
        """Reuse the existing Windows sensor provider, without a new process per frame."""
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            return
        command = [shell, '-NoProfile', '-NonInteractive', '-File',
                   str(core.PROJECT / 'dashboard/live_state.ps1'), '-IntervalMs', '3000', '-Count', '0', '-Warmup']
        with self._extra_lock:
            if self._closed:
                return
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             text=True, encoding='utf-8', errors='replace',
                                             creationflags=core.NO_WINDOW)
            process = self._process
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                    with self._extra_lock:
                        self._extra = {k: value.get(k) for k in ('temp_c', 'gpu_pct', 'vol_pct', 'top_proc', 'top_proc_pct')}
                        self._extra['_at'] = time.monotonic()
                except (ValueError, TypeError):
                    continue
        finally:
            process.stdout.close()
            process.wait()

    def close(self):
        with self._extra_lock:
            self._closed = True
            process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def read(self):
        if sys.platform == 'win32' and not self._collecting:
            self._collecting = True
            threading.Thread(target=self._enrich, daemon=True).start()
        now = dt.datetime.now()
        mem = psutil.virtual_memory()
        net, tick = psutil.net_io_counters(), time.monotonic()
        elapsed = max(0.01, tick - self.tick)
        down = max(0, net.bytes_recv - self.net.bytes_recv) * 8 / elapsed / 1e6
        up = max(0, net.bytes_sent - self.net.bytes_sent) * 8 / elapsed / 1e6
        self.net, self.tick = net, tick
        bat = psutil.sensors_battery()
        freq = psutil.cpu_freq()
        seconds = max(0, int(time.time() - psutil.boot_time()))
        ip = next((a.address for name, values in psutil.net_if_addrs().items()
                   if psutil.net_if_stats().get(name) and psutil.net_if_stats()[name].isup
                   for a in values if a.family == socket.AF_INET and not a.address.startswith('127.')), None)
        try:
            sensors = getattr(psutil, 'sensors_temperatures', lambda: {})() or {}
        except (OSError, RuntimeError):
            sensors = {}
        cpu_sensors = [e.current for name, values in sensors.items()
                       if any(key in name.lower() for key in ('cpu', 'coretemp', 'k10temp'))
                       for e in values if e.current is not None]
        result = dict(clock=now.strftime('%H:%M:%S'), date=now.strftime('%Y-%m-%d'),
                    host=socket.gethostname(), ip=ip,
                    uptime=f'{seconds // 86400}d {seconds // 3600 % 24:02}:{seconds // 60 % 60:02}',
                    cpu_load=psutil.cpu_percent(interval=0.15), cpu_freq_ghz=freq.current / 1000 if freq else None,
                    mem_pct=mem.percent, mem_used_gb=mem.used / 2**30, mem_total_gb=mem.total / 2**30,
                    disk_free_gb=psutil.disk_usage(str(Path.home().anchor)).free / 2**30,
                    net_down_mbps=down, net_up_mbps=up,
                    bat_pct=bat.percent if bat else None, bat_ac=bat.power_plugged if bat else True,
                    bat_charging=bool(bat and bat.power_plugged and bat.percent < 100),
                    temp_c=max(cpu_sensors) if cpu_sensors else None, gpu_pct=None,
                    vol_pct=None, top_proc=None, top_proc_pct=None)
        with self._extra_lock:
            if time.monotonic() - self._extra.get('_at', 0) < 12:
                result.update({k: v for k, v in self._extra.items() if k != '_at'})
        return result


class Api:
    def __init__(self, directory=None, engine=None, renderer=None, sampler=None):
        self._core = engine or core
        self._renderer = renderer or load_renderer()
        self._sampler = sampler or Sampler()
        self._dir = Path(directory or user_directory())
        self._dir.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._render_lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._window = None
        self._log = []
        self._preview = []
        self._revision = 0
        self._registry = self._renderer.registry_json()
        self._config = {'widgets': list(self._registry['presets']['minimal']), 'interval': 5}
        self._last = None
        try:
            config = json.loads((self._dir / 'settings.json').read_text(encoding='utf-8'))
            self._config = self._validate(config)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        try:
            last = json.loads((self._dir / 'last-session.json').read_text(encoding='utf-8'))
            # Journal is not a source of arbitrary paths. Resolve only our own session directory.
            sid = last['id']
            if len(sid) == 32 and all(c in '0123456789abcdef' for c in sid):
                self._last = self._dir / 'backups' / sid
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._state = dict(busy=False, kind=None, phase='idle', title='准备就绪',
                           message='连接键盘后即可开始。', progress=0, connected=False,
                           connection='未检测', stream=False, sent=0, restore=bool(self._last),
                           backup=None, error=False)

    def _validate(self, config):
        ids = config['widgets']
        known = {w['id'] for w in self._registry['widgets']}
        if not isinstance(ids, list) or len(ids) > len(known) or any(not isinstance(x, str) or x not in known for x in ids):
            raise ValueError('组件配置无效。')
        if len(set(ids)) != len(ids):
            raise ValueError('组件不能重复。')
        interval = config.get('interval', 5)
        if interval not in (3, 5, 10):
            raise ValueError('刷新间隔无效。')
        return {'widgets': ids, 'interval': interval}

    def _event(self, **values):
        with self._mutex:
            self._state.update(values)

    def _line(self, line):
        with self._mutex:
            self._log.append(str(line).rstrip())
            self._log = self._log[-1500:]
            try:
                with (self._dir / 'diagnostics.log').open('a', encoding='utf-8') as log:
                    log.write(str(line).rstrip() + '\n')
            except OSError:
                pass  # A diagnostic write must never interrupt an in-flight flash.

    def _job(self, kind, fn):
        with self._mutex:
            if self._state['busy']:
                return {'ok': False, 'message': '请等待当前操作结束，或先停止小组件。'}
            self._state.update(busy=True, kind=kind, error=False, phase='working', progress=0)
            self._stop.clear()
            def run():
                try:
                    fn()
                except Exception as exc:
                    self._line(str(exc))
                    self._event(phase='error', title='操作未完成', error=True,
                                message=self._friendly(exc))
                finally:
                    self._event(busy=False, stream=False, kind=None)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
        return {'ok': True}

    def _friendly(self, exc):
        text = str(exc)
        if any(x in text for x in ('需且仅需', '未找到', '屏幕DFU不可用', '设备不匹配')):
            return '未能连接键盘。请用 USB 数据线直连，关闭官方程序后重试；详细原因见诊断。'
        if '不匹配' in text or '未知固件' in text:
            return '这个固件暂不支持自动补丁，已停止写入。请保留自动备份并提交诊断。'
        return '操作已停止。请打开诊断查看原因；如写入曾经开始，请保留 USB 连接并使用备份恢复。'

    def bootstrap(self):
        return dict(version=self._core.VERSION, registry=self._registry,
                    config=copy.deepcopy(self._config), backupDirectory=str(self._dir / 'backups'),
                    sensorNote='温度、GPU、音量取决于系统数据源；不可用时显示 n/a。')

    def snapshot(self, revision=-1):
        with self._mutex:
            data = dict(state=copy.deepcopy(self._state), revision=self._revision)
            if revision != self._revision:
                data['previews'] = list(self._preview)
            return data

    def diagnostics(self):
        with self._mutex:
            return {'text': '\n'.join(self._log) or '暂无诊断记录。', 'directory': str(self._dir)}

    def configure(self, config):
        with self._mutex:
            if self._state['busy']:
                return {'ok': False, 'message': '请先停止当前操作再修改组件。'}
            try:
                valid = self._validate(config)
                atomic_json(self._dir / 'settings.json', valid)
                self._config = valid
                return {'ok': True, 'config': copy.deepcopy(valid)}
            except (ValueError, KeyError, TypeError, OSError) as exc:
                return {'ok': False, 'message': str(exc)}

    def connect(self):
        def work():
            self._event(title='正在检测键盘', message='请保持 USB 连接。', connected=False, connection='未连接')
            info = json.loads(self._core.node_run('wallpaper/screen_wallpaper.js', ['status']))
            if info.get('vendorId') != 0x5542 or info.get('productId') != 1 or info.get('width') != 480 or info.get('height') != 360:
                raise ValueError('设备不匹配')
            self._event(connected=True, connection='USB 已连接', title='键盘已连接',
                        message='全屏补丁和小组件已就绪。', phase='idle')
        return self._job('connect', work)

    def _make_frames(self, state):
        ids = list(self._config['widgets'])
        pages = self._renderer.paginate(ids)
        history = self._renderer.append_history(str(self._dir / 'history'), state)
        frames = [self._renderer.render(state, widgets=page, quality=72, limit=30000, history=history)[0] for page in pages]
        with self._mutex:
            self._preview = ['data:image/jpeg;base64,' + base64.b64encode(frame).decode('ascii') for frame in frames]
            self._revision += 1
        return frames

    def preview(self):
        def work():
            self._event(title='正在更新预览', message='只读取电脑状态，不向键盘发送。')
            with self._render_lock:
                self._make_frames(self._sampler.read())
            self._event(title='预览已更新', message='预览来自这台电脑的实际数据。', phase='idle')
        return self._job('preview', work)

    def patch(self, confirmed=False, force=False):
        if confirmed is not True or not isinstance(force, bool):
            return {'ok': False, 'message': '需要确认固件操作。'}
        def work():
            sid = uuid.uuid4().hex
            folder = self._dir / 'backups' / sid
            folder.mkdir(parents=True)
            baseline, target = folder / 'original.bin', folder / 'fullscreen.bin'
            self._event(title='正在备份原始固件', message='自动双读校验，请勿断开 USB。', progress=8, phase='backup', backup=str(folder))
            raw = self._core.node_run('tools/mcu_backup.js', [str(baseline), '--yes'], on_line=self._line)
            report = json.loads(raw.splitlines()[-1])
            if not report.get('complete') or report.get('bytes') != 491520 or not baseline.is_file():
                raise ValueError('备份未完成，禁止写入。')
            if not report.get('appReturned'):
                raise ValueError('备份完整，但尚未确认回到应用模式。请检查连接后重试。')
            if report.get('firmwareKind') == 'already-fullscreen':
                self._event(phase='done', title='已经是全屏固件', message='无需重复应用补丁，已保留本次备份。', progress=100)
                return
            if not report.get('patchable') and not force:
                raise ValueError('未知固件，已保留备份，未执行写入。')
            self._event(title='正在匹配全屏补丁', message='保留本机配置，生成对应补丁。', progress=35, phase='prepare')
            result = self._core.generate(baseline, target, force=force)
            self._line(json.dumps(result, ensure_ascii=False))
            # Persist recovery before the first possible erase, even if the flash later fails.
            atomic_json(folder / 'session.json', {'force': force, 'created': dt.datetime.now().isoformat(), 'result': result})
            atomic_json(self._dir / 'last-session.json', {'id': sid})
            self._last = folder
            self._event(restore=True, title='正在应用并校验', message='请勿拔线或退出。每个写入区都会逐字节回验。', progress=50, phase='flash')
            self._core.maintain('flash', baseline, target, yes=True, force=force, on_line=self._line)
            self._event(title='正在重新连接', message='写入校验通过，正在返回键盘界面。', progress=95, phase='reconnect')
            self._core.node_run('tools/leave_mcu.js', on_line=self._line)
            self._event(title='全屏补丁已完成', message='现在可以在官方程序导入全屏图片和 GIF。', phase='done', progress=100)
        return self._job('patch', work)

    def restore(self, confirmed=False):
        if confirmed is not True or self._last is None:
            return {'ok': False, 'message': '需要有效备份并确认恢复。'}
        def work():
            folder = self._last
            session = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
            if not isinstance(session.get('force'), bool):
                raise ValueError('恢复记录无效，未连接设备。')
            self._core.validate_firmware(folder / 'original.bin', folder / 'fullscreen.bin', force=session['force'])
            self._event(title='正在检查现场固件', message='确认与这份备份对应，请勿拔线。', phase='restore', progress=8)
            current = folder / ('before-restore-' + uuid.uuid4().hex + '.bin')
            self._core.node_run('tools/mcu_backup.js', [str(current), '--yes'], on_line=self._line)
            original = (folder / 'original.bin').read_bytes()
            target = (folder / 'fullscreen.bin').read_bytes()[:491520]
            live = current.read_bytes()
            if len(live) != 491520 or len(original) != 491520 or len(target) != 491520:
                raise ValueError('现场固件不完整，停止恢复。')
            # Only changed 8K protection groups may differ (including a failed partial write).
            for offset in range(0, len(original), 8192):
                end = min(offset + 8192, len(original))
                if original[offset:end] == target[offset:end] and live[offset:end] != original[offset:end]:
                    raise ValueError('现场固件与此备份不匹配，停止恢复。')
            self._event(title='正在恢复备份', message='恢复最近一次应用补丁前的固件，请勿拔线。', progress=35)
            self._core.maintain('restore', folder / 'original.bin', folder / 'fullscreen.bin',
                                yes=True, force=session['force'], on_line=self._line)
            self._core.node_run('tools/leave_mcu.js', on_line=self._line)
            self._event(title='备份已恢复', message='已逐字节回验并请求返回键盘界面。', phase='done', progress=100)
        return self._job('restore', work)

    def start(self):
        if not self._config['widgets']:
            return {'ok': False, 'message': '请先添加至少一个组件。'}
        def work():
            self._event(stream=True, sent=0, title='正在连接屏幕', message='请关闭其他上传程序。', phase='stream')
            bridge = self._core.dash()
            page, sent = 0, 0
            while not self._stop.is_set():
                begun = time.monotonic()
                with self._render_lock:
                    frames = self._make_frames(self._sampler.read())
                if self._stop.is_set():
                    break
                image = self._dir / 'current.jpg'
                image.write_bytes(frames[page % len(frames)])
                bridge.push_once('push', image=image, on_line=self._line, timeout=45)
                sent += 1
                page += 1
                self._event(sent=sent, connected=True, connection='USB 已连接', title='正在同步小组件',
                            message='已发送，请在键盘屏幕确认显示。', progress=0)
                self._stop.wait(max(0.1, self._config['interval'] - (time.monotonic() - begun)))
            self._event(title='同步已停止', message='键盘保留最后发送的画面。', phase='idle')
        return self._job('stream', work)

    def stop(self):
        with self._mutex:
            if self._state['kind'] != 'stream':
                return {'ok': False, 'message': '当前没有组件同步任务。'}
            self._stop.set()
            self._event(title='正在停止同步', message='等待当前图片发送结束后停止。')
        return {'ok': True}

    def open_backups(self):
        folder = self._dir / 'backups'
        folder.mkdir(exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(folder))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(folder)])
        return {'ok': True}

    def _closing(self):
        # Never allow closing during a firmware job or in-flight USB transfer.
        with self._mutex:
            if self._state['busy']:
                if self._state['kind'] == 'stream':
                    self._stop.set()
                self._event(message='请等待当前操作结束后关闭窗口。')
                return False
        close = getattr(self._sampler, 'close', None)
        if close:
            close()
        return True


def run(smoke=False):
    import webview
    temporary = tempfile.TemporaryDirectory(prefix='neos75-smoke-') if smoke else None
    api = Api(directory=temporary.name if temporary else None)
    window = webview.create_window('Neos75', str(core.PROJECT / 'desktop/ui/index.html'),
                                   js_api=api, width=1100, height=900, min_size=(740, 620),
                                   background_color='#edf2f8', text_select=False, hidden=smoke)
    api._window = window
    window.events.closing += api._closing
    result = {}
    def check():
        try:
            for _ in range(150):
                version = window.evaluate_js("document.getElementById('version')?.textContent")
                if version == 'v' + core.VERSION:
                    result['ok'] = True
                    break
                time.sleep(0.1)
            if not result.get('ok'):
                result['error'] = 'webview bridge failed to initialize'
            else:
                count = window.evaluate_js("document.querySelectorAll('#widget-list li').length")
                if count < 1:
                    result['error'] = 'widget registry not rendered'
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            window.destroy()
    if smoke:
        window.events.loaded += check
    webview.start(debug=False, private_mode=True, gui='edgechromium' if sys.platform == 'win32' else None,
                  icon=str(core.PROJECT / ('desktop/ui/icon.ico' if sys.platform == 'win32' else 'desktop/ui/icon.png')))
    if smoke:
        temporary.cleanup()
        if result.get('error') or not result.get('ok'):
            raise RuntimeError(result.get('error', 'webview did not load'))
        return {'result': 'PASS', 'gui': 'real webview, local UI, Python bridge and widget gallery initialized', 'device_write': False}
