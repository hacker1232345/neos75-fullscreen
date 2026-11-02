"""Reproducible Windows x64 single-file build (run npm ci first)."""
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

ROOT=Path(__file__).resolve().parents[1]
VERSION=json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']

def build_renderer(node, dist, work):
    """把 dashboard/render.py 单独冻结成一个可执行渲染器，放进 runtime/。

    桌面程序不能假设目标机器装了 Python + Pillow，所以渲染器自带一份解释器和
    Pillow。它的命令行与 dashboard/render.py 完全一致，因此 dashboard/push.js
    和 desktop/dash_bridge.py 只需把「解释器 + 脚本」换成这个 exe。
    """
    spec=work/'Neos75-Render.spec'
    datas=[(str(ROOT/'dashboard/widgets.json'),'dashboard')]
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
    if sys.platform not in ['win32','darwin']:raise SystemExit('Build on Windows or macOS.')
    node=shutil.which('node')
    if not node:raise SystemExit('Node.js is required on the build machine.')
    if not (ROOT/'node_modules/node-hid').exists():raise SystemExit('Run npm ci before building.')
    subprocess.run([node,str(ROOT/'desktop/native_check.js')],cwd=ROOT,check=True)
    a.dist=a.dist.resolve();a.work=a.work.resolve()
    a.dist.mkdir(parents=True,exist_ok=True);a.work.mkdir(parents=True,exist_ok=True)
    # Include the runtime's own license; source builds obtain it from the official release.
    version=subprocess.check_output([node,'--version'],text=True).strip()
    node_arch=subprocess.check_output([node,'-p','process.arch'],text=True).strip()
    license_file=a.work/'NODE-LICENSE.txt'
    if not license_file.exists():
        license_url=f'https://raw.githubusercontent.com/nodejs/node/{version}/LICENSE'
        try:
            with urllib.request.urlopen(license_url) as r:license_file.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            major=version.split('.')[0]
            license_url=f'https://raw.githubusercontent.com/nodejs/node/{major}.x/LICENSE'
            with urllib.request.urlopen(license_url) as r:license_file.write_bytes(r.read())
        (a.work/'NODE-LICENSE-source.txt').write_text(license_url,encoding='utf-8')
    datas=[(str(node),'runtime'),(str(license_file),'licenses')]
    # 随包渲染器：桌面版渲染屏幕部件时用它，不依赖目标机器的 Python/Pillow
    renderer=build_renderer(node,a.dist,a.work)
    datas.append((str(renderer),'runtime'))
    for folder in ['tools','wallpaper','firmware','examples','docs','dashboard','desktop','node_modules']:
        # Explicit allowlist: never collect the repo root, local backups, or build products.
        for f in sorted((ROOT/folder).rglob('*')):
            if not f.is_file() or '__pycache__' in f.parts:continue
            if folder=='node_modules' and 'prebuilds' in f.parts:
                prebuild=f.parts[f.parts.index('prebuilds')+1]
                if prebuild!=f'HID-{sys.platform}-{node_arch}':continue
            if folder=='dashboard' and '.history' in f.parts:continue  # 采样历史是运行时产物
            if folder=='desktop' and f.suffix=='.py' and f.name!='dash_bridge.py':continue  # 只带桥接层，不带构建脚本
            if folder!='node_modules' and (f.suffix in ['.bin','.pyc'] or f.name.endswith('.rollback.json')):continue
            dest=Path('project')/f.relative_to(ROOT).parent
            datas.append((str(f),dest.as_posix()))
    for f in [ROOT/'desktop/native_check.js',ROOT/'LICENSE',ROOT/'THIRD_PARTY_NOTICES.md',ROOT/'README.md',ROOT/'package.json']:
        dest='project/desktop' if f.name=='native_check.js' else 'project'
        datas.append((str(f),dest))
    spec=a.work/'Neos75-Fullscreen.spec'
    spec_text='a = Analysis('+repr([str(ROOT/'desktop/app.py')])+', pathex=[], binaries=[], datas='+repr(datas)+', hiddenimports=[], excludes=[])\npyz = PYZ(a.pure)\n'
    if sys.platform=='win32':
        spec_text+="exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Neos75-Fullscreen', debug=False, strip=False, upx=False, console=False)\n"
    else:
        spec_text+="exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Neos75-Fullscreen', debug=False, strip=False, upx=False, console=False)\n"
        spec_text+="coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Neos75-Fullscreen')\n"
        spec_text+="app = BUNDLE(coll, name='Neos75-Fullscreen.app', bundle_identifier='io.github.hacker1232345.neos75fullscreen', info_plist={'CFBundleShortVersionString':'"+VERSION+"','NSHighResolutionCapable':True,'LSMinimumSystemVersion':'15.0'})\n"
    spec.write_text(spec_text,encoding='utf-8')
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(a.dist),'--workpath',str(a.work/'pyinstaller'),str(spec)]
    subprocess.run(cmd,cwd=ROOT,check=True)
    exe=a.dist/('Neos75-Fullscreen.exe' if sys.platform=='win32' else 'Neos75-Fullscreen.app/Contents/MacOS/Neos75-Fullscreen')
    info={'version':VERSION,'platform':sys.platform+'-'+platform.machine(),'bytes':exe.stat().st_size,'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'node':version,'pyinstaller':subprocess.check_output([sys.executable,'-m','PyInstaller','--version'],text=True).strip()}
    (a.dist/'Neos75-Fullscreen.sha256.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    print(json.dumps(info))

if __name__=='__main__':main()
