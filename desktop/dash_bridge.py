"""屏幕部件（dashboard）桥接层：把 dashboard/ 的采样、渲染、推送能力接到桌面上。

设计约束：
  * 本模块只做编排，不含任何固件/DFU 逻辑，也不碰固件文件；
  * 全部走子进程调用 dashboard/ 下的脚本，因此 GUI 进程不需要 PIL；
  * 注册表（部件清单、预设）通过 `render.py --list` 读取，单一事实来源留在 dashboard/。

所以这里能做的事都是可离线验证的：--list / 渲染一张静态图。真正推送需要 USB 直连键盘。
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class DashboardError(RuntimeError):
    pass


def _project():
    # 冻结（PyInstaller）时项目根在 _MEIPASS/project，否则是本文件的上一级。
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'project'
    return Path(__file__).resolve().parents[1]


def _python():
    """渲染脚本要用的解释器：源码运行时就是当前解释器。"""
    return sys.executable or 'python'


def _renderer():
    """冻结版随包附带的独立渲染器（内含 Pillow）。

    打包出来的程序不能假设目标机器装了 Python + Pillow，所以 build_exe.py 会把
    dashboard/render.py 单独冻结成一个可执行文件放进 runtime/，这里优先用它。
    源码运行时返回 None，走「解释器 + 脚本路径」的老路。
    """
    if not getattr(sys, 'frozen', False):
        return None
    names = ('Neos75-Render.exe', 'Neos75-Render')
    bases = [Path(getattr(sys, '_MEIPASS', '.'))]
    if sys.executable:
        bases.append(Path(sys.executable).resolve().parent)
    for base in bases:
        for name in names:
            for cand in (base / 'runtime' / name, base / name):
                if cand.exists():
                    return str(cand)
    return None


def _render_command(args):
    """构造渲染调用：冻结版 `[渲染器, *args]`，源码版 `[python, dashboard/render.py, *args]`。"""
    exe = _renderer()
    if exe:
        return [exe, *args]
    return [_python(), 'dashboard/render.py', *args]


def _pwsh():
    for name in ('pwsh', 'powershell'):
        found = shutil.which(name)
        if found:
            return found
    raise DashboardError('找不到 PowerShell（pwsh / powershell），无法采样电脑状态。')


def _python_env():
    """render.py 会往 stdout 打带 U+2714 的完成行；Windows 默认 GBK 控制台会直接抛
    UnicodeEncodeError，所以强制子进程用 UTF-8 输出（我们这边也按 UTF-8 读）。"""
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return env


def _run(command, timeout=None, cwd=None, env=None):
    env = _python_env() if env is None else env
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    env.setdefault('PYTHONUTF8', '1')
    env.pop('NODE_OPTIONS', None)
    env.pop('NEOS75_NODE_MODULES', None)
    try:
        p = subprocess.run(command, cwd=str(cwd or _project()), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    except FileNotFoundError as e:
        raise DashboardError('命令不可用：%s（%s）' % (command[0], e)) from None
    except subprocess.TimeoutExpired:
        raise DashboardError('命令超时：%s' % ' '.join(map(str, command))) from None
    if p.returncode:
        detail = (p.stderr or p.stdout or '').strip().splitlines()
        raise DashboardError(detail[-1] if detail else '子程序失败，退出码 %d' % p.returncode)
    return p.stdout


# ── 离线：注册表 ──────────────────────────────────────────────────
def registry():
    """读 dashboard/ 的部件注册表（canvas / presets / groups / widgets）。"""
    out = _run(_render_command(['--list']))
    data = json.loads(out)
    if not isinstance(data, dict) or 'widgets' not in data:
        raise DashboardError('部件注册表格式不符合预期')
    return data


def widget_ids(data=None):
    data = registry() if data is None else data
    return [w['id'] for w in data['widgets']]


def default_widgets(data=None):
    """注册表里 default=true 的部件，即 GUI 首次打开时勾选的那批。"""
    data = registry() if data is None else data
    return [w['id'] for w in data['widgets'] if w.get('default')]


def grouped(data=None):
    """按 group 归组的 (组名, [部件]) 列表，保持注册表顺序。"""
    data = registry() if data is None else data
    out = []
    for w in data['widgets']:
        g = w.get('group') or '其他'
        if not out or out[-1][0] != g:
            out.append((g, []))
        out[-1][1].append(w)
    return out


# ── 离线：采样与渲染 ──────────────────────────────────────────────
def sample(count=1, interval_ms=1500, warmup=True, on_line=None):
    """只读采样：跑 live_state.ps1，返回最后一次的完整状态字典。

    带 -Warmup 时第 0 轮强制刷新全部 20 个字段（一次性约 +0.6s），所以 count=1
    也能拿到完整一行；不带则头几轮 net/gpu/temp 为 null，需要多跑几轮取最后一行。
    """
    command = [_pwsh(), '-NoProfile', '-NonInteractive', '-File',
               str(_project() / 'dashboard/live_state.ps1'),
               '-IntervalMs', str(int(interval_ms)), '-Count', str(int(count))]
    if warmup:
        command.append('-Warmup')
    p = subprocess.run(command, cwd=str(_project()), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    rows = []
    for line in (p.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('{'):
            rows.append(line)
            if on_line:
                on_line(line)
    if not rows:
        raise DashboardError('状态采样没有返回数据（PowerShell 输出为空）。')
    try:
        return json.loads(rows[-1])
    except ValueError as e:
        raise DashboardError('状态 JSON 解析失败：%s' % e) from None


def render(state_path, out_path, widgets=None, preset='default', quality=72, limit=None,
           preview=None, state_dir=None, on_line=None):
    """调 dashboard/render.py 把状态 JSON 画成 480x360 baseline JPEG。离线，不碰设备。"""
    command = _render_command(['--in', str(state_path), '--out', str(out_path),
                               '--preset', preset, '--quality', str(int(quality))])
    if widgets:
        command += ['--widgets', ','.join(widgets)]
    if limit:
        command += ['--limit', str(int(limit))]
    if preview:
        command += ['--preview', str(preview)]
    if state_dir:
        command += ['--state-dir', str(state_dir)]
    text = _run(command)
    if on_line:
        on_line(text.strip())
    return text.strip()


def selftest(on_line=None):
    """dashboard/selftest.js：纯离线自测，不打开 USB。"""
    script = _project() / 'dashboard' / 'selftest.js'
    if not script.exists():
        raise DashboardError('dashboard/selftest.js 尚未提供。')
    text = _node(['dashboard/selftest.js'])
    if on_line:
        on_line(text.strip())
    return text.strip()


def _node(args):
    node = _node_path()
    env = os.environ.copy()
    env['NODE_PATH'] = str(_project() / 'node_modules')
    env.pop('NEOS75_NODE_MODULES', None)
    env.pop('NODE_OPTIONS', None)
    # 冻结版把随包渲染器交给 selftest.js，否则它会去找目标机器上的 python。
    exe = _renderer()
    if exe:
        env['NEOS75_RENDER'] = exe
    return _run([node, *args], env=env)


def _node_path():
    if getattr(sys, 'frozen', False):
        candidate = Path(sys._MEIPASS) / 'runtime' / ('node.exe' if sys.platform == 'win32' else 'node')
        if candidate.exists():
            return str(candidate)
    found = shutil.which('node') or 'node'
    return found


def push_command(mode, image=None, widgets=None, preset=None, quality=None, limit=None,
                 interval=None, state_dir=None, as_json=True):
    """构造 dashboard/push.js 的命令行（不执行）。

    mode: info / dry / once / loop / push
      info  只读查询屏幕（不开写通道）
      dry   离线构造传输计划，完全不打开 USB 设备
      once  采样 → 渲染 → 推送一帧
      loop  连续采样推送（直到停止）
      push  把已有 JPEG 推上去
    退出码：0 成功 / 1 运行失败 / 2 参数错误 / 3 设备不匹配
    """
    args = [_node_path(), 'dashboard/push.js', mode]
    if mode == 'push':
        if not image:
            raise DashboardError('push 模式需要指定图片路径。')
        args.append(str(image))
    if widgets:
        args += ['--widgets', ','.join(widgets)]
    if preset:
        args += ['--preset', preset]
    if quality is not None:
        args += ['--quality', str(int(quality))]
    if limit is not None:
        args += ['--limit', str(int(limit))]
    if interval is not None:
        args += ['--interval', str(int(interval))]
    if state_dir:
        args += ['--state-dir', str(state_dir)]
    if as_json:
        args.append('--json')
    return args


def push_env():
    """push.js 需要的环境（NODE_PATH 指向项目内 node_modules，允许覆盖解释器）。"""
    env = os.environ.copy()
    env['NODE_PATH'] = str(_project() / 'node_modules')
    env.pop('NEOS75_NODE_MODULES', None)
    env.pop('NODE_OPTIONS', None)
    if 'NEOS75_PYTHON' not in env:
        env['NEOS75_PYTHON'] = _python()
    exe = _renderer()
    if exe:
        env['NEOS75_RENDER'] = exe
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return env


def push_once(mode='once', on_line=None, timeout=None, **kw):
    """跑一次即结束的 push.js 命令（info / dry / once / push），返回 stdout 全文。"""
    script = _project() / 'dashboard' / 'push.js'
    if not script.exists():
        raise DashboardError('dashboard/push.js 尚未提供。')
    command = push_command(mode, **kw)
    env = push_env()
    p = subprocess.run(command, cwd=str(_project()), env=env, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    text = p.stdout or ''
    if on_line:
        for line in text.splitlines():
            on_line(line)
    if p.returncode:
        raise DashboardError('push.js %s 失败（退出码 %d）：%s' % (
            mode, p.returncode, (text.strip().splitlines() or [''])[-1]))
    return text


def native_probe(timeout=6, on_line=None):
    """只查原生组件在当前页是否就绪（读 RAM，不写入）。"""
    script = _project() / 'tools' / 'push_widget.js'
    if not script.exists():
        raise DashboardError('tools/push_widget.js 尚未提供。')
    text = _run([_node_path(), str(script), 'probe'], env=native_env(), timeout=timeout)
    if on_line:
        on_line(text.strip())
    try:
        return json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise DashboardError('原生组件探测没有返回可解析结果：%s' % text.strip()) from None


def native_env():
    """原生文字组件通道需要的环境（NODE_PATH + 清理继承来的 Node 变量）。"""
    env = push_env()
    env.pop('PYTHONIOENCODING', None)
    env.pop('PYTHONUTF8', None)
    return env


def native_frame(rows, mask=None):
    """把四行 ASCII 与可选页掩码编成 push_widget.js loop 读的一行 JSON。

    rows=None 表示这一行只写页掩码，不改动当前显示的文字。
    """
    payload = {}
    if rows is not None:
        if not isinstance(rows, list) or len(rows) != 4:
            raise DashboardError('原生组件需要正好四行文字。')
        payload['rows'] = [str(r) for r in rows]
    if mask is not None:
        # mask=0 是合法的：表示六页全不显示，等于把文字组件关掉（设备实测回 0xFE）。
        if isinstance(mask, bool) or not isinstance(mask, int) or not 0 <= mask <= 0x3f:
            raise DashboardError('页掩码必须是 0..0x3f 的整数（0 表示关闭）。')
        payload['mask'] = mask
    return json.dumps(payload, ensure_ascii=False)


def native_popen(on_line=None):
    """以长驻子进程打开原生文字通道，返回 Popen。

    子进程从 stdin 逐行读 {"rows": [...], "mask": n}，每读到一行就写一次 RAM。
    这条通道不擦 Flash、不进 DFU，也不占用 JPEG/GIF 上传队列。
    """
    script = _project() / 'tools' / 'push_widget.js'
    if not script.exists():
        raise DashboardError('tools/push_widget.js 尚未提供。')
    p = subprocess.Popen([_node_path(), str(script), 'loop'], cwd=str(_project()),
                         env=native_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                         creationflags=NO_WINDOW, bufsize=0)
    return p


def native_mask(mask, timeout=20, on_line=None):
    """只写页掩码：决定哪些界面显示文字组件（写 RAM，不写 Flash）。"""
    if isinstance(mask, bool) or not isinstance(mask, int) or not 0 <= mask <= 0x3f:
        raise DashboardError('页掩码必须是 0..0x3f 的整数（0 表示关闭）。')
    return native_once(None, mask=mask, timeout=timeout, on_line=on_line, frames_only=False)


def native_close(process, timeout=3):
    """关掉常驻原生通道：先结束 stdin，再等它自己退出。"""
    if process is None:
        return None
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return process.returncode


def native_once(rows, mask=None, timeout=20, on_line=None, frames_only=True):
    """推一帧就退出：把同一个 loop 通道喂一行然后结束 stdin。"""
    p = native_popen()
    try:
        out, err = p.communicate(native_frame(rows, mask) + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise DashboardError('原生组件写入超时，设备无应答。') from None
    if on_line:
        for line in (out or '').splitlines():
            on_line(line)
        for line in (err or '').splitlines():
            if line.strip():
                on_line(line)
    frames = masks = 0
    for line in (out or '').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get('error'):
            raise DashboardError('原生组件写入失败：%s' % value['error'])
        if value.get('frame'):
            frames += 1
        elif value.get('mask'):
            masks += 1
    if frames_only and not frames:
        raise DashboardError('原生组件没有任何应答：%s' % ((err or '').strip().splitlines() or [''])[-1])
    return {'frames': frames, 'masks': masks, 'displayVerified': False}


def push_popen(mode='loop', on_line=None, **kw):
    """以长驻子进程启动连续推送（loop），返回 Popen；调用方负责 terminate。"""
    script = _project() / 'dashboard' / 'push.js'
    if not script.exists():
        raise DashboardError('dashboard/push.js 尚未提供。')
    command = push_command(mode, **kw)
    p = subprocess.Popen(command, cwd=str(_project()), env=push_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    return p
