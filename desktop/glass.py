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
from data_sources import Sources, location, search_cities, THREAD_ID


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


def load_native_rows():
    """原生文字通道的行构造器（与 dashboard 共用同一份 widgets.json）。"""
    spec = importlib.util.spec_from_file_location('neos75_native_rows', core.PROJECT / 'dashboard/native_rows.py')
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
        try:
            freq = getattr(psutil, 'cpu_freq', lambda: None)()
        except (OSError, NotImplementedError, RuntimeError):
            freq = None
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
    def __init__(self, directory=None, engine=None, renderer=None, sampler=None, sources=None):
        self._core = engine or core
        self._renderer = renderer or load_renderer()
        self._sampler = sampler or Sampler()
        self._sources = sources or Sources()
        self._display_page = None
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
        self._rows = load_native_rows()
        self._config = {'widgets': list(self._registry['presets']['minimal']), 'interval': 5,
                        'native_pages': [], 'native_interval': 3}
        self._native = {'ready': None, 'rows': ['', '', '', ''], 'omitted': [],
                        'supported': [w['id'] for w in self._registry['widgets']
                                      if w['id'] in self._rows.TEXT_WIDGETS],
                        'unsupported': [w['id'] for w in self._registry['widgets']
                                        if w['id'] not in self._rows.TEXT_WIDGETS]}
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
                           backup=None, error=False, native=False, nativeSent=0,
                           nativeReady=None)

    @staticmethod
    def _page_selection(pages):
        """原生组件的页面选择：1..6，允许为空（表示不启用原生组件）。"""
        if not isinstance(pages, list):
            raise ValueError('原生组件页面无效：应为页面列表。')
        cleaned = []
        for value in pages:
            if type(value) is not int or not 1 <= value <= 6:
                raise ValueError('原生组件页面无效：请选择 1 到 6 之间的界面。')
            if value in cleaned:
                raise ValueError('原生组件页面不能重复。')
            cleaned.append(value)
        return sorted(cleaned)

    @staticmethod
    def _native_interval(value):
        if value not in (2, 3, 5, 10):
            raise ValueError('原生组件刷新间隔无效。')
        return value

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
        valid = {'widgets': ids, 'interval': interval,
                 'native_pages': self._page_selection(config.get('native_pages', [])),
                 'native_interval': self._native_interval(config.get('native_interval', 3))}
        if 'weather' in config:
            valid['weather'] = location(config['weather'])
        if 'codex_thread' in config:
            thread = config['codex_thread']
            if not isinstance(thread, str) or (thread and not THREAD_ID.fullmatch(thread)):
                raise ValueError('Codex 任务 ID 无效，留空可自动跟随最近任务。')
            valid['codex_thread'] = thread
        return valid

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
            if kind not in ('native-stream',):
                self._state['native'] = False
            def run():
                try:
                    fn()
                except Exception as exc:
                    self._line(str(exc))
                    self._event(phase='error', title='操作未完成', error=True,
                                message=self._friendly(exc))
                finally:
                    with self._mutex:
                        self._native_process = None
                    self._event(busy=False, stream=False, kind=None)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
        return {'ok': True}

    def _friendly(self, exc):
        text = str(exc)
        # 适配器给出的已经是给用户看的人话（说明是哪一层卡住、为什么），
        # 不能再用一句「操作已停止」把它盖掉 —— 那等于又把话说回去了。
        if any(x in text for x in ('签名对不上', '差得比较远', '可能已经打过补丁',
                                   '把小组件要用到的那几个厂商函数改过了', '为安全起见没有写入')):
            return text
        if any(x in text for x in ('需且仅需', '未找到', '屏幕DFU不可用', '设备不匹配')):
            return '未能连接键盘。请用 USB 数据线直连，关闭官方程序后重试；详细原因见诊断。'
        if '不匹配' in text or '未知固件' in text:
            return '这个固件暂不支持自动补丁，已停止写入。请保留自动备份并提交诊断。'
        return '操作已停止。请打开诊断查看原因；如写入曾经开始，请保留 USB 连接并使用备份恢复。'

    def bootstrap(self):
        with self._mutex:
            native = copy.deepcopy(self._native)
        return dict(version=self._core.VERSION, registry=self._registry,
                    config=copy.deepcopy(self._config), backupDirectory=str(self._dir / 'backups'),
                    native=native,
                    nativeNote='文字组件直接写入键盘内存：每屏四行、只支持英文与数字，中文会显示为空。',
                    sensorNote='温度、GPU、音量取决于系统数据源；不可用时显示 n/a。')

    def snapshot(self, revision=-1):
        with self._mutex:
            data = dict(state=copy.deepcopy(self._state), revision=self._revision, displayPage=self._display_page,
                        text=list(self._native['rows']))
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
                self._display_page = None
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

    def search_city(self, query):
        try:
            return {'ok': True, 'cities': search_cities(query)}
        except (OSError, ValueError, KeyError, TypeError):
            return {'ok': False, 'message': '未能搜索城市，请检查网络或换用城市拼音。'}

    def select_page(self, page=None):
        with self._mutex:
            count = len(self._renderer.paginate(self._config['widgets']))
            if page is not None and (type(page) is not int or not 0 <= page < count):
                return {'ok': False, 'message': '组件页不存在，请先更新预览。'}
            self._display_page = page
            return {'ok': True, 'displayPage': page}

    def screen_page(self, direction, confirmed=False):
        if confirmed is not True or direction not in ('previous', 'next'):
            return {'ok': False, 'message': '请选择上一界面或下一界面。'}
        def work():
            self._core.node_run('wallpaper/pages.js', [direction, '--yes'])
            self._event(title='已发送界面切换', message='请在键盘确认。切换原厂界面不会新增媒体存储槽。', phase='idle')
        return self._job('screen-page', work)

    def _read_state(self):
        state = self._sampler.read()
        state.update(self._sources.read(self._config['widgets'], self._config))
        return state

    def _native_rows(self, state=None):
        """把当前组件选择折成四行 ASCII；未选的组件不会出现在屏幕上。"""
        state = state if state is not None else self._read_state()
        rows, omitted = self._rows.rows_from_state(state, self._config['widgets'])
        with self._mutex:
            self._native.update(rows=rows, omitted=omitted)
        return rows, omitted

    def native_preview(self):
        """只在这台电脑上生成四行文字，不向键盘发送任何字节。"""
        def work():
            self._event(title='正在生成文字画面', message='只读取电脑状态，不向键盘发送。')
            rows, omitted = self._native_rows()
            self._event(title='文字画面已生成',
                        message=('有 %d 个组件无法写成文字，已略过。' % len(omitted)) if omitted
                        else '四行文字已按选择的组件生成。')
        return self._job('native-preview', work)

    def native_probe(self):
        """读一次设备的原生组件状态（只读 RAM，不写 Flash、不进 DFU）。"""
        def work():
            self._event(title='正在检查原生组件', message='读取键盘当前界面状态。')
            bridge = self._core.dash()
            if not hasattr(bridge, 'native_probe'):
                raise ValueError('原生组件通道不可用，请更新程序。')
            info = bridge.native_probe()
            ready = bool(info.get('ready'))
            with self._mutex:
                self._native['ready'] = ready
            if ready:
                self._event(nativeReady=True, title='原生组件已就绪',
                            message='当前界面可以接收文字，直接同步即可。', phase='idle')
            else:
                self._event(nativeReady=False, title='当前界面还没有组件',
                            message='这个界面尚未初始化：先切到已选中的界面，再同步一次即可。', phase='idle')
        return self._job('native-probe', work)

    def native_pages(self):
        """把「显示在哪些界面」写进键盘（写 RAM，不写 Flash）。

        一个界面都不选也是合法选择：掩码 0 会把文字组件彻底关掉，原厂页面恢复干净。
        """
        mask = self._native_mask()
        pages = self._config.get('native_pages') or []
        def work():
            if mask:
                self._event(title='正在应用界面选择', message='第 %s 屏将显示文字组件。'
                            % '、'.join(str(p) for p in pages), phase='native')
            else:
                self._event(title='正在关闭文字组件', message='所有界面都不再叠文字。', phase='native')
            bridge = self._core.dash()
            result = bridge.native_mask(mask, on_line=self._line)
            if mask:
                self._event(native=True, nativeSent=result.get('masks', 0), title='界面选择已应用',
                            message='已写入键盘内存：第 %s 屏。翻到这些界面即可看到文字。'
                                    % '、'.join(str(p) for p in pages), phase='done')
            else:
                self._event(native=False, nativeSent=result.get('masks', 0),
                            nativeReady=False, title='文字组件已关闭',
                            message='键盘内存里的四行文字已清空，所有界面恢复原厂内容。', phase='done')
        return self._job('native-pages', work)

    def native_push(self):
        """采样 → 四行文字 → 推一帧到键盘当前界面。"""
        def work():
            self._event(title='正在发送文字画面', message='文字直接写入键盘内存，不擦除固件。', phase='native')
            rows, _ = self._native_rows()
            bridge = self._core.dash()
            result = bridge.native_once(rows, on_line=self._line)
            self._event(native=True, nativeSent=result.get('frames', 0), connected=True,
                        connection='USB 已连接', title='文字画面已发送',
                        message='键盘已确认收到，请看屏确认。', phase='done')
        return self._job('native-push', work)

    def _native_mask(self):
        """页掩码；0 = 一个界面都不显示（等于关闭文字组件）。"""
        mask = 0
        for page in self._config.get('native_pages') or []:
            mask |= 1 << (page - 1)
        return mask

    def native_start(self):
        if not self._config['widgets']:
            return {'ok': False, 'message': '请先添加至少一个组件。'}
        def work():
            bridge = self._core.dash()
            mask = self._native_mask()
            pages = self._config.get('native_pages') or []
            if not mask:
                raise ValueError('当前没有选中任何界面：先在上面勾选要显示文字的界面，文字组件才会开始同步。')
            self._event(native=True, nativeSent=0, phase='native', title='正在打开文字通道',
                        message='第 %s 屏会显示文字组件。' % '、'.join(str(p) for p in pages))
            process = bridge.native_popen()
            self._native_process = process
            try:
                self._write_native(process, {'mask': mask})
                self._line('原生组件已启用：第 %s 屏' % '、'.join(str(p) for p in pages))
                sent = 0
                interval = self._config.get('native_interval', 3)
                while not self._stop.is_set():
                    begun = time.monotonic()
                    rows, omitted = self._native_rows()
                    reply = self._write_native(process, {'rows': rows})
                    if reply.get('error'):
                        # The device rejects frames on a page that has not been initialised;
                        # keep the channel open so it starts working once the user turns there.
                        self._line('这一帧未被接受：%s' % reply['error'])
                        self._event(title='等待切到已选中的界面',
                                    message='当前界面没有原生组件：翻到第 %s 屏后会自动开始显示。'
                                            % '、'.join(str(p) for p in pages))
                        self._stop.wait(max(0.2, interval - (time.monotonic() - begun)))
                        continue
                    sent += 1
                    self._event(nativeSent=sent, connected=True, connection='USB 已连接',
                                title='正在同步文字组件',
                                message=('已发送 %d 帧 · 有 %d 个组件无法写成文字' % (sent, len(omitted)))
                                if omitted else '已发送 %d 帧，请看屏确认。' % sent)
                    self._stop.wait(max(0.2, interval - (time.monotonic() - begun)))
            finally:
                self._native_process = None
                bridge.native_close(process)
                self._line('原生组件通道已关闭。')
            self._event(native=False, title='文字同步已停止',
                        message='键盘保留最后收到的四行文字。', phase='idle')
        return self._job('native-stream', work)

    def _write_native(self, process, payload):
        """向常驻通道写一行并读回一行应答；超时即视为设备不再应答。"""
        line = json.dumps(payload, ensure_ascii=False) + '\n'
        process.stdin.write(line)
        process.stdin.flush()
        deadline = time.monotonic() + 8
        while True:
            reply = process.stdout.readline()
            if not reply:
                raise ValueError('原生组件通道已断开，请重新连接键盘。')
            try:
                value = json.loads(reply)
            except ValueError:
                self._line(reply.strip())
                if time.monotonic() > deadline:
                    raise ValueError('原生组件没有应答。') from None
                continue
            return value

    def stop(self):
        with self._mutex:
            if self._state['kind'] == 'native-stream':
                self._stop.set()
                self._event(title='正在停止文字同步', message='等待当前帧写入结束。')
                return {'ok': True}
            if self._state['kind'] != 'stream':
                return {'ok': False, 'message': '当前没有组件同步任务。'}
            self._stop.set()
            self._event(title='正在停止同步', message='等待当前图片发送结束后停止。')
        return {'ok': True}

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
                self._make_frames(self._read_state())
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
                # 哈希不认识不等于没救：生成这一步会按内容锚点现算偏移。
                # 只有适配器确实一个字节都套不上时，generate() 才会抛出并写明原因。
                self._line('固件版本不在已知清单里，改用内容锚点现算补丁位置。')
            self._event(title='正在匹配全屏补丁', message='保留本机配置，生成对应补丁。', progress=35, phase='prepare')
            result = self._core.generate(baseline, target, force=force)
            self._line(json.dumps(result, ensure_ascii=False))
            if result.get('adapted'):
                # 别在这里就承诺「进度条会被去掉」——去进度条的那一层在链中间，
                # 适配器可能只套上了后面的注入层。等它把话说明白再讲。
                removed = result.get('progressRemoved')
                message = ('这份固件的布局和已知版本不一样，程序按内容锚点重新算出了每一处补丁的位置。'
                           if removed is not False else
                           '这份固件的布局和已知版本不一样，程序按内容锚点重新算出了补丁位置，'
                           '但没能套上「去掉上传进度条」那一层：浏览器上传时进度条会保留。')
                self._event(title='已按锚点自动适配', message=message, progress=40, phase='prepare')
            # 强制模式的真实下限：一层补丁的旧字节都对不上时，只能盲套最基础的一层，
            # 而「去掉上传进度条」那几条补丁在更后面的一层里。既然用户已经勾了强制，
            # 就继续刷，但必须先把这件事说明白，不能让刷完才发现进度条还在。
            if result.get('forceBlind'):
                self._line('警告：这把键盘的固件布局与已知版本全都对不上，只套了最基础的一层。')
                self._event(title='进度条不会被去掉', message='布局对不上，只套了最基础的一层补丁：'
                            '浏览器上传时的进度条和覆盖层会保留，其余功能正常。完整备份已保留。')
            # Persist recovery before the first possible erase, even if the flash later fails.
            atomic_json(folder / 'session.json', {'force': force, 'created': dt.datetime.now().isoformat(), 'result': result})
            atomic_json(self._dir / 'last-session.json', {'id': sid})
            self._last = folder
            self._event(restore=True, title='正在应用并校验', message='请勿拔线或退出。每个写入区都会逐字节回验。', progress=50, phase='flash')
            self._core.maintain('flash', baseline, target, yes=True, force=force, on_line=self._line)
            self._event(title='正在重新连接', message='写入校验通过，正在返回键盘界面。', progress=95, phase='reconnect')
            self._core.node_run('tools/leave_mcu.js', on_line=self._line)
            # 三种情况都要如实：强制盲套、适配时漏了去进度条那一层、以及全都到位。
            kept = bool(result.get('forceBlind')) or result.get('progressRemoved') is False
            finished = '补丁已完成（进度条仍在）' if kept else '全屏补丁已完成'
            self._event(title=finished, message='现在可以在官方程序导入全屏图片和 GIF。', phase='done', progress=100)
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
                    frames = self._make_frames(self._read_state())
                if self._stop.is_set():
                    break
                image = self._dir / 'current.jpg'
                with self._mutex:
                    selected = self._display_page
                index = page % len(frames) if selected is None else min(selected, len(frames) - 1)
                image.write_bytes(frames[index])
                bridge.push_once('push', image=image, on_line=self._line, timeout=45)
                sent += 1
                page += 1
                self._event(sent=sent, connected=True, connection='USB 已连接', title='正在同步小组件',
                            message='已发送，请在键盘屏幕确认显示。', progress=0)
                self._stop.wait(max(0.1, self._config['interval'] - (time.monotonic() - begun)))
            self._event(title='同步已停止', message='键盘保留最后发送的画面。', phase='idle')
        return self._job('stream', work)

    def open_backups(self):
        folder = self._dir / 'backups'
        folder.mkdir(exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(folder))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(folder)])
        else:
            subprocess.Popen(['xdg-open', str(folder)])
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
    if sys.platform.startswith('linux'):
        if os.geteuid() == 0:
            raise RuntimeError('请以普通用户启动界面，使用专用 udev 规则配置 USB 权限。')
        os.environ['QT_API'] = 'pyside6'
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
                # Exercise Pillow + psutil inside the main frozen app, not only
                # the separate renderer. This reads host metrics, never USB.
                with api._render_lock:
                    frames = api._make_frames(api._read_state())
                if not frames or not frames[0].startswith(b'\xff\xd8'):
                    result['error'] = 'real-metrics preview did not render'
                extra = api._renderer.render({'weather': {'label': '请选择城市'}, 'codex': {'label': '暂无新事件'}},
                                             widgets=['weather', 'codex'])[0]
                if not extra.startswith(b'\xff\xd8'):
                    result['error'] = 'weather/Codex widgets did not render'
                # The native text channel must render its four rows without touching USB.
                buttons = window.evaluate_js("document.querySelectorAll('#native-pages button').length")
                if buttons != 6:
                    result['error'] = 'native page chooser not rendered'
                rows = api.native_preview() and [api._thread.join(6), api.snapshot().get('text')][1]
                if not rows or len(rows) != 4:
                    result['error'] = 'native text rows not generated'
                text = window.evaluate_js("document.querySelectorAll('#native-preview span').length")
                if text != 4:
                    result['error'] = 'native text preview not rendered'
                # 「关闭文字组件」入口必须存在，且空选必须映射到掩码 0（这是关闭的唯一机制）。
                off = window.evaluate_js("document.getElementById('native-off')?.textContent")
                if not off:
                    result['error'] = 'native off switch not rendered'
                if api._page_selection([]) != []:
                    result['error'] = 'empty page selection rejected'
                if api._native_mask() != 0:
                    result['error'] = 'empty page selection did not map to mask 0'
                if api._page_selection([2, 4, 6]) != [2, 4, 6]:
                    result['error'] = 'page selection lost its order'
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            window.destroy()
    if smoke:
        window.events.loaded += check
    engine = 'edgechromium' if sys.platform == 'win32' else ('qt' if sys.platform.startswith('linux') else None)
    webview.start(debug=False, private_mode=True, gui=engine,
                  icon=str(core.PROJECT / ('desktop/ui/icon.ico' if sys.platform == 'win32' else 'desktop/ui/icon.png')))
    if smoke:
        temporary.cleanup()
        if result.get('error') or not result.get('ok'):
            raise RuntimeError(result.get('error', 'webview did not load'))
        return {'result': 'PASS', 'gui': 'real webview, local UI, Python bridge and widget gallery initialized', 'device_write': False}
