import http.server
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.error
import urllib.request

root = Path(os.environ['CONTROL_RECEIPTS'])
root.mkdir(parents=True, exist_ok=True)
lock = threading.Lock()
barrier = None

def event(value):
    with lock:
        with (root / 'mirror-events.jsonl').open('a') as output:
            output.write(json.dumps({'at': time.time(), **value}) + '\n')

class Mirror(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        global barrier
        if self.path == '/control-ready':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ready')
            return
        with lock:
            if barrier is None:
                barrier = time.monotonic() + 90
            wait = max(0, barrier - time.monotonic())
        event({'phase': 'request', 'path': self.path, 'controlledDelaySeconds': wait})
        time.sleep(wait)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request('https://archive.ubuntu.com' + self.path)
        for header in ['Range', 'If-Modified-Since', 'If-Range']:
            if self.headers.get(header):
                request.add_header(header, self.headers[header])
        try:
            try:
                response = opener.open(request, timeout=180)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in ['connection', 'transfer-encoding', 'server', 'date']:
                        self.send_header(key, value)
                self.end_headers()
                size = 0
                while data := response.read(262144):
                    self.wfile.write(data)
                    size += len(data)
                event({'phase': 'served', 'path': self.path, 'bytes': size, 'status': response.status})
        except Exception as error:
            event({'phase': 'error', 'path': self.path, 'error': str(error)})
            self.send_error(502)


def configure():
    files = [Path('/etc/apt/apt-mirrors.txt'), Path('/etc/apt/sources.list')]
    files += list(Path('/etc/apt/sources.list.d').glob('*.list'))
    files += list(Path('/etc/apt/sources.list.d').glob('*.sources'))
    changes = []
    for file in files:
        if file.is_file():
            before = file.read_text()
            after = re.sub(r'https?://(?:azure\.)?archive\.ubuntu\.com/ubuntu',
                           'http://azure.archive.ubuntu.com/ubuntu', before)
            file.write_text(after)
            changes.append({'file': str(file), 'before': before, 'controlled': after})
    assert any('http://azure.archive.ubuntu.com/ubuntu' in x['controlled'] for x in changes)
    (root / 'apt-sources.json').write_text(json.dumps(changes, indent=2))
    with Path('/etc/hosts').open('a') as output:
        output.write('\n127.0.0.1 azure.archive.ubuntu.com\n')


def verify(mode):
    events = [json.loads(line) for line in (root / 'mirror-events.jsonl').read_text().splitlines()] if (root / 'mirror-events.jsonl').exists() else []
    requests = [row for row in events if row['phase'] == 'request']
    debs = [row for row in events if row['phase'] == 'served' and '.deb' in row['path'] and row['status'] in [200, 206] and row['bytes'] > 0]
    if mode == 'baseline':
        assert requests and max(row['controlledDelaySeconds'] for row in requests) >= 89
        assert debs, 'Baseline must fetch actual dependency packages through controlled mirror'
    else:
        assert not requests, 'Prepared install must bypass the delayed Azure mirror entirely'
        assert 'https://archive.ubuntu.com/ubuntu' in (root / 'install.log').read_text()
    assert not [row for row in events if row['phase'] == 'error']
    (root / 'mirror-review.json').write_text(json.dumps({'mode': mode, 'requests': len(requests), 'dependencyPackagesServed': len(debs), 'controlledDelaySeconds': max([row['controlledDelaySeconds'] for row in requests], default=0), 'source': os.environ['CONTROL_SOURCE'], 'kind': 'Controlled delayed mirror, not a natural day-wide performance sample'}, indent=2))

if __name__ == '__main__':
    if sys.argv[1] == 'configure':
        configure()
    elif sys.argv[1] == 'serve':
        http.server.ThreadingHTTPServer(('127.0.0.1', 80), Mirror).serve_forever()
    elif sys.argv[1] == 'verify':
        verify(sys.argv[2])
