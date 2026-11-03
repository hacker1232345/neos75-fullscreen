"""Check a Linux distribution without configuring host USB permissions."""
from pathlib import Path
import os
import subprocess
import sys

bundle = Path(sys.argv[1] if len(sys.argv) > 1 else 'dist/Neos75-Fullscreen').resolve()
internal = bundle / '_internal'
for relative in ['Neos75', 'Neos75-Fullscreen', 'install-usb-access.sh',
                 '_internal/runtime/node', '_internal/runtime/Neos75-Render',
                 '_internal/PySide6/Qt/libexec/QtWebEngineProcess']:
    path = bundle / relative
    assert path.is_file() and os.access(path, os.X_OK), 'Missing executable: ' + str(path)
for script in ['Neos75', 'install-usb-access.sh']:
    subprocess.run(['/bin/sh', '-n', str(bundle / script)], check=True)
for name in ['HID-linux-x64', 'HID_hidraw-linux-x64']:
    folder = internal / 'project/node_modules/node-hid/prebuilds' / name
    assert any(folder.glob('*.node')), 'Missing native binding: ' + name
assert (internal / 'fonts/NotoSansCJK-Regular.ttc').is_file(), 'Chinese font missing'
assert (bundle / '70-neos75.rules').is_file(), 'USB permission rule missing'
print('PASS Linux bundle: launcher, Qt helper, both HID backends, runtimes, CJK font, udev rule; no USB opened')
