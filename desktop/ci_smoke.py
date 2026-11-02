"""Test the built app, with external runtimes removed from PATH."""
from pathlib import Path
import json,os,subprocess,sys,tempfile
root=Path(__file__).resolve().parents[1]
exe=root/('dist/Neos75-Fullscreen.exe' if sys.platform=='win32' else 'dist/Neos75-Fullscreen.app/Contents/MacOS/Neos75-Fullscreen')
env=os.environ.copy();env['PATH']='';env.pop('NODE_PATH',None);env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
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
print('PASS bundled runtime, native modules, GUI startup, unconfirmed flash gate; no USB device required')
