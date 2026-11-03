"""Local visual-review harness. Intentionally has NO firmware / USB endpoints."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import mimetypes
import sys

from glass import Api

UI = Path(__file__).parent / 'ui'
temp = tempfile.TemporaryDirectory(prefix='neos75-ui-review-')
api = Api(directory=temp.name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content='application/json', code=200):
        self.send_response(code)
        self.send_header('Content-Type', content + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        name = self.path.split('?')[0]
        if name == '/':
            data = (UI / 'index.html').read_text(encoding='utf-8')
            data = data.replace('</head>', '<script src="review.js" defer></script></head>')
            return self.send(data.encode(), 'text/html')
        if name == '/review.js':
            script = """window.neos75Preview = new Proxy({}, {get:(_,name)=>(...args)=>fetch('/review/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())});window.dispatchEvent(new Event('neos75previewready'));"""
            return self.send(script.encode(), 'text/javascript')
        if name in ('/app.js', '/style.css'):
            return self.send((UI / name[1:]).read_bytes(), mimetypes.guess_type(name)[0] or 'text/plain')
        self.send(b'Not found', 'text/plain', 404)

    def do_POST(self):
        # JSON + exact same-origin header prevent cross-site form submissions.
        origin = self.headers.get('Origin')
        if self.headers.get('Content-Type') != 'application/json' or (origin and origin != 'http://' + self.headers.get('Host', '')):
            return self.send(b'{}', code=403)
        length = int(self.headers.get('Content-Length', '0'))
        if length > 32768:
            return self.send(b'{}', code=413)
        args = json.loads(self.rfile.read(length))
        method = self.path.removeprefix('/review/')
        allowed = {'bootstrap', 'snapshot', 'diagnostics', 'configure', 'preview'}
        if method in allowed:
            result = getattr(api, method)(*args)
        else:
            result = {'ok': False, 'message': '界面测试模式：USB 与固件操作已禁用。'}
        self.send(json.dumps(result, ensure_ascii=False).encode())


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 39276
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print('UI review: http://127.0.0.1:%d (no USB endpoints)' % server.server_port, flush=True)
    try:
        server.serve_forever()
    finally:
        api._closing()
        server.server_close()
        temp.cleanup()
