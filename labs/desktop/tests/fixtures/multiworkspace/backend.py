"""No model or user configuration: HTTP lifecycle fixture in a temporary workspace."""
import http.server, json, os, secrets, sys, threading
from pathlib import Path
workspace = sys.argv[sys.argv.index('--workspace') + 1]
state_path = Path(workspace) / 'lifecycle-state.json'
state = json.loads(state_path.read_text(encoding='utf-8', errors='replace')) if state_path.exists() else {}
log = Path(os.environ['WORKSPACE_TEST_ROOT']) / 'backend-events.jsonl'

def record(event):
    with log.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'event': event, 'workspace': workspace, 'pid': os.getpid(), 'port': server.server_port}) + '\n')
HTML = "<!doctype html><meta charset=utf-8><title>Workspace fixture</title><body><h1>原生多工作台隔离验证</h1><script>\nconst config=window.__XIAOYU_DESKTOP__;\nwindow.testState={session_id:config.session_id||'',page:config.page||'sessions',draft:''};\nwindow.__XIAOYU_VIEW_STATE__=()=>window.testState;\nwindow.__XIAOYU_WORK_STATUS__=()=>({running:0,pending:0});\nwindow.__XIAOYU_FLUSH__=async()=>{await new Promise(resolve=>setTimeout(resolve,100));await fetch('/state/'+config.view_id,{method:'POST',body:JSON.stringify(window.testState)})};\nfetch('/state/'+config.view_id).then(x=>x.json()).then(s=>{Object.assign(window.testState,s);window.fixtureReady=true;document.body.append(config.view_id)});\n</script>"

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def do_GET(self):
        payload = HTML.encode() if self.path == '/workbench/' else json.dumps(state.get(self.path, {})).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if self.path == '/workbench/' else 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        state[self.path] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        state_path.write_text(json.dumps(state), encoding='utf-8')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
record('start')
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/workbench/', 'token': secrets.token_hex(32)}), flush=True)
sys.stdin.read()
record('stop')
server.shutdown()
server.server_close()
