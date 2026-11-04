"""Build native Windows, macOS and Linux desktop bundles (run npm ci first)."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
import platform
import os
import time
from importlib.metadata import distribution, PackageNotFoundError
from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT=Path(__file__).resolve().parents[1]
VERSION=json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']


def hid_prebuilds(target, arch):
    names = {f'HID-{target}-{arch}'}
    if target == 'linux':
        names.add(f'HID_hidraw-{target}-{arch}')
    return names


def linux_fonts():
    if not sys.platform.startswith('linux'):
        return []
    font = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if not font.is_file():
        raise SystemExit('Install fonts-noto-cjk before building the Linux package.')
    result = [(str(font), 'fonts')]
    license_file = Path('/usr/share/doc/fonts-noto-cjk/copyright')
    if license_file.exists():
        result.append((str(license_file), 'licenses/NotoSansCJK'))
    return result


def node_license(node, version, work):
    """Prefer the exact installed Node distribution's license, avoiding CI DNS."""
    destination = work / 'NODE-LICENSE.txt'
    executable = Path(node).resolve()
    candidates = [executable.parent / 'LICENSE']
    if executable.parent.name == 'bin':
        candidates.append(executable.parent.parent / 'LICENSE')
    for candidate in candidates:
        if candidate.is_file() and 'Node.js' in candidate.read_text(encoding='utf-8'):
            shutil.copy2(candidate, destination)
            (work / 'NODE-LICENSE-source.txt').write_text(str(candidate), encoding='utf-8')
            return destination
    url = f'https://raw.githubusercontent.com/nodejs/node/{version}/LICENSE'
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            if b'Node.js' not in data:
                raise ValueError('Unexpected Node license response')
            destination.write_bytes(data)
            (work / 'NODE-LICENSE-source.txt').write_text(url, encoding='utf-8')
            return destination
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

def build_renderer(node, dist, work):
    """把 dashboard/render.py 单独冻结成一个可执行渲染器，放进 runtime/。

    桌面程序不能假设目标机器装了 Python + Pillow，所以渲染器自带一份解释器和
    Pillow。它的命令行与 dashboard/render.py 完全一致，因此 dashboard/push.js
    和 desktop/dash_bridge.py 只需把「解释器 + 脚本」换成这个 exe。
    """
    spec=work/'Neos75-Render.spec'
    datas=[(str(ROOT/'dashboard/widgets.json'),'dashboard'), *linux_fonts()]
    spec_text=('a = Analysis('+repr([str(ROOT/'dashboard/render.py')])+', pathex=[], binaries=[], '
               'datas='+repr(datas)+', hiddenimports=[], excludes=[])\n'
               'pyz = PYZ(a.pure)\n')
    spec_text+="exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Neos75-Render', debug=False, strip=False, upx=False, console=True)\n"
    spec.write_text(spec_text,encoding='utf-8')
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(work/'renderer-dist'),
                    '--workpath',str(work/'renderer-build'),str(spec)],cwd=ROOT,check=True)
    exe=work/'renderer-dist'/('Neos75-Render.exe' if sys.platform=='win32' else 'Neos75-Render')
    if not exe.exists():raise SystemExit('渲染器构建失败：%s 不存在'%exe)
    return exe

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dist',type=Path,default=ROOT/'dist')
    p.add_argument('--work',type=Path,default=ROOT/'build')
    a=p.parse_args()
    if sys.platform not in ['win32','darwin','linux']:raise SystemExit('Build on Windows, macOS or Linux.')
    if sys.platform == 'linux':
        os.environ['QT_API'] = 'pyside6'
    node=shutil.which('node')
    if not node:raise SystemExit('Node.js is required on the build machine.')
    if not (ROOT/'node_modules/node-hid').exists():raise SystemExit('Run npm ci before building.')
    subprocess.run([node,str(ROOT/'desktop/native_check.js')],cwd=ROOT,check=True)
    a.dist=a.dist.resolve();a.work=a.work.resolve()
    a.dist.mkdir(parents=True,exist_ok=True);a.work.mkdir(parents=True,exist_ok=True)
    # Include the runtime's own license; source builds obtain it from the official release.
    version=subprocess.check_output([node,'--version'],text=True).strip()
    node_arch=subprocess.check_output([node,'-p','process.arch'],text=True).strip()
    license_file=node_license(node, version, a.work)
    datas=[(str(node),'runtime'),(str(license_file),'licenses'), *linux_fonts()]
    binaries=[]  # 运行时用 importlib 加载的模块（keystone/capstone）靠它带上动态库
    for name in ['pywebview', 'psutil', 'Pillow', 'pythonnet', 'clr_loader',
                 'proxy_tools', 'bottle', 'cffi', 'pycparser', 'PySide6',
                 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'QtPy']:
        try:
            package_dist = distribution(name)
        except PackageNotFoundError:
            continue  # pythonnet/clr_loader are Windows-only webview dependencies.
        entries = [f for f in (package_dist.files or []) if Path(f).name.upper() in ('LICENSE', 'LICENSE.TXT', 'COPYING') or 'licenses/' in str(f).lower()]
        if not entries:
            entries = [f for f in (package_dist.files or []) if str(f).endswith('.dist-info/METADATA')]
        datas.extend((str(package_dist.locate_file(f)), 'licenses/' + name + '/' + str(f.parent)) for f in entries)
    # 随包渲染器：桌面版渲染屏幕部件时用它，不依赖目标机器的 Python/Pillow
    renderer=build_renderer(node,a.dist,a.work)
    datas.append((str(renderer),'runtime'))
    for folder in ['tools','wallpaper','firmware','examples','docs','dashboard','desktop','node_modules']:
        # Explicit allowlist: never collect the repo root, local backups, or build products.
        for f in sorted((ROOT/folder).rglob('*')):
            if not f.is_file() or '__pycache__' in f.parts:continue
            if folder=='node_modules' and 'node-hid' in f.parts and 'prebuilds' in f.parts:
                prebuild=f.parts[f.parts.index('prebuilds')+1]
                if prebuild not in hid_prebuilds(sys.platform, node_arch):continue
            if folder=='dashboard' and '.history' in f.parts:continue  # 采样历史是运行时产物
            if folder=='desktop' and f.suffix=='.py' and f.name!='dash_bridge.py':continue  # 只带桥接层，不带构建脚本
            if folder!='node_modules' and (f.suffix in ['.bin','.pyc'] or f.name.endswith('.rollback.json')):continue
            dest=Path('project')/f.relative_to(ROOT).parent
            datas.append((str(f),dest.as_posix()))
    for f in [ROOT/'desktop/native_check.js',ROOT/'LICENSE',ROOT/'THIRD_PARTY_NOTICES.md',ROOT/'README.md',ROOT/'package.json']:
        dest='project/desktop' if f.name=='native_check.js' else 'project'
        datas.append((str(f),dest))
    spec=a.work/'Neos75-Fullscreen.spec'
    # The webview hook collects WebView2/WKWebView runtime pieces. Pillow/psutil
    # are explicit because the dashboard renderer is loaded from a fixed path.
    # tools/build_widget.py 是在运行时用 importlib 加载的（适配未知固件时要重新汇编注入层），
    # PyInstaller 的静态分析看不见它里面的 `from keystone import Ks` 和 `from capstone import Cs`。
    # 少了这两个包，打包版一碰到不认识的固件就会死于 "No module named 'keystone'"。
    # 动态库要和模块放在同一层：这两个包都按自己 __file__ 的位置去找 .dll。
    # keystone 是可选件：PyPI 不给 macOS arm64 出 wheel，那颗 runner 上装不了。
    # 少了它，注入层没法重新汇编（工具会说明并只套前面几层），但程序照样要能打包出去。
    missing = []
    for module in ('keystone', 'capstone'):
        try:
            binaries.extend(collect_dynamic_libs(module))
        except Exception:                         # pragma: no cover - 平台差异
            missing.append(module)
    if 'capstone' in missing:
        raise SystemExit('打包需要 capstone：先 pip install -r requirements-dev.txt')
    hidden = ['glass', 'webview', 'psutil', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont']
    hidden += [m for m in ('keystone', 'capstone') if m not in missing]
    if sys.platform == 'linux':
        hidden += ['webview.platforms.qt', 'qtpy', 'PySide6.QtWebEngineCore',
                   'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel', 'PySide6.QtNetwork']
    spec_text='a = Analysis('+repr([str(ROOT/'desktop/app.py')])+', pathex='+repr([str(ROOT/'desktop')])+', binaries='+repr(binaries)+', datas='+repr(datas)+', hiddenimports='+repr(hidden)+', excludes=[])\npyz = PYZ(a.pure)\n'
    if sys.platform=='win32':
        spec_text+="exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Neos75-Fullscreen', icon="+repr(str(ROOT/'desktop/ui/icon.ico'))+", debug=False, strip=False, upx=False, console=False)\n"
    elif sys.platform == 'darwin':
        spec_text+="exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Neos75-Fullscreen', debug=False, strip=False, upx=False, console=False)\n"
        spec_text+="coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Neos75-Fullscreen')\n"
        spec_text+="app = BUNDLE(coll, name='Neos75-Fullscreen.app', icon="+repr(str(ROOT/'desktop/ui/icon.icns'))+", bundle_identifier='io.github.hacker1232345.neos75fullscreen', info_plist={'CFBundleShortVersionString':'"+VERSION+"','NSHighResolutionCapable':True,'LSMinimumSystemVersion':'15.0'})\n"
    else:
        spec_text+="exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Neos75-Fullscreen', debug=False, strip=False, upx=False, console=False)\n"
        spec_text+="coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Neos75-Fullscreen')\n"
    spec.write_text(spec_text,encoding='utf-8')
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(a.dist),'--workpath',str(a.work/'pyinstaller'),str(spec)]
    subprocess.run(cmd,cwd=ROOT,check=True)
    if sys.platform == 'linux':
        bundle = a.dist/'Neos75-Fullscreen'
        for file in (ROOT/'desktop/linux').iterdir():
            if file.is_file():
                shutil.copy2(file, bundle/file.name)
        for name in ['Neos75', 'install-usb-access.sh', '_internal/runtime/node', '_internal/runtime/Neos75-Render']:
            path = bundle/name
            path.chmod(path.stat().st_mode | 0o111)
    executable = {'win32':'Neos75-Fullscreen.exe', 'darwin':'Neos75-Fullscreen.app/Contents/MacOS/Neos75-Fullscreen', 'linux':'Neos75-Fullscreen/Neos75-Fullscreen'}
    exe=a.dist/executable[sys.platform]
    info={'version':VERSION,'platform':sys.platform+'-'+platform.machine(),'bytes':exe.stat().st_size,'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'node':version,'pyinstaller':subprocess.check_output([sys.executable,'-m','PyInstaller','--version'],text=True).strip()}
    (a.dist/'Neos75-Fullscreen.sha256.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    print(json.dumps(info))

if __name__=='__main__':main()
