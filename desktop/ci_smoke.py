"""Test the built app, with external runtimes removed from PATH."""
from pathlib import Path
import json,os,shutil,subprocess,sys,tempfile
root=Path(__file__).resolve().parents[1]
paths={'win32':'dist/Neos75-Fullscreen.exe','darwin':'dist/Neos75-Fullscreen.app/Contents/MacOS/Neos75-Fullscreen','linux':'dist/Neos75-Fullscreen/Neos75-Fullscreen'}
exe=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else root/paths[sys.platform]
# PyInstaller 的 onefile 引导会检查路径是否"安全"：路径里出现会影响 DLL 搜索的证据，
# 或目录本身不给当前用户建临时目录的权限时，它会直接拒绝解包。CI 上路径永远是干净的
# ASCII，但本地仓库常常放在带权限收紧或非 ASCII 的目录里 —— 那种情况下先在
# 系统临时目录里跑一份副本，并在这里把它讲清楚，避免误判成程序缺陷。
staged=None
if sys.platform=='win32':
    probe=str(exe)
    if any(ord(c)>127 for c in probe):
        stage=Path(tempfile.gettempdir())/'neos75-ci-smoke'
        stage.mkdir(parents=True,exist_ok=True)
        staged=stage/exe.name
        shutil.copy2(exe,staged)
        print('note: 原路径含非 ASCII 字符，已复制到 %s 再测（PyInstaller onefile 引导限制，与程序无关）'%staged)
        exe=staged
env=os.environ.copy();env['PATH']='';env.pop('NODE_PATH',None);env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
if sys.platform=='linux':
    env['QT_API']='pyside6'
try:
    with tempfile.TemporaryDirectory() as t:
        for label,arg in [('selftest','--self-test'),('gui','--gui-smoke')]:
            result=Path(t)/(label+'.json')
            subprocess.run([str(exe),arg,'--result-file',str(result)],env=env,check=True)
            data=json.loads(result.read_text(encoding='utf-8'))
            assert data['ok'] and data['result']=='PASS',data
            print(label,json.dumps(data,ensure_ascii=True))
        bad=Path(t)/'bad.json'
        p=subprocess.run([str(exe),'--maintain','flash','--baseline','missing','--target','missing','--result-file',str(bad)],env=env)
        assert p.returncode!=0 and not json.loads(bad.read_text(encoding='utf-8'))['ok']
        source=Path(t)/'unknown.bin';source.write_bytes(bytes(491520))
        target=Path(t)/'forced.bin';report=Path(t)/'forced.json'
        p=subprocess.run([str(exe),'--build',str(source),'--out',str(target),'--result-file',str(report)],env=env)
        assert p.returncode!=0 and not target.exists()
        subprocess.run([str(exe),'--build',str(source),'--out',str(target),'--force','--result-file',str(report)],env=env,check=True)
        data=json.loads(report.read_text(encoding='utf-8'));assert data['ok'] and data['force']
        assert target.stat().st_size==491536 and source.read_bytes()==bytes(491520)
        p=subprocess.run([str(exe),'--maintain','flash','--baseline',str(source),'--target',str(target),'--force','--result-file',str(bad)],env=env)
        assert p.returncode!=0 and not json.loads(bad.read_text(encoding='utf-8'))['ok']
    print('PASS bundled runtime, native modules, GUI startup, unconfirmed flash gate; no USB device required')
finally:
    if staged and staged.exists():staged.unlink()

