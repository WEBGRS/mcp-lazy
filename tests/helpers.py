import glob
import json
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAZY = os.path.join(ROOT, 'lazy_mcp.py')
FAKE = os.path.join(ROOT, 'tests', 'fake_server.py')
sys.path.insert(0, ROOT)


def env_for(home):
    return dict(os.environ, LAZY_MCP_HOME=home)


class Client:
    """Speaks to a lazy_mcp.py proxy over stdio like Claude Code would."""

    def __init__(self, home, marker, idle=None, name='fake', server=None):
        cmd = [sys.executable, '-I', LAZY, name] + (['--idle', str(idle)] if idle is not None else []) + \
              ['--'] + (server or [sys.executable, FAKE, marker])
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env_for(home))
        self.q = queue.Queue()
        self.notes = []
        self.seq = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.q.put(json.loads(line))
        self.q.put(None)

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + '\n').encode())
        self.p.stdin.flush()

    def recv(self, timeout=10):
        msg = self.q.get(timeout=timeout)
        if msg is None:
            raise EOFError('proxy closed stdout')
        return msg

    def request(self, method, params=None, timeout=10, on_request=None):
        self.seq += 1
        mid = self.seq
        self.send({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            msg = self.recv(max(0.05, deadline - time.monotonic()))
            if msg.get('id') == mid and 'method' not in msg:
                return msg
            if 'method' in msg and 'id' in msg and on_request:
                self.send({'jsonrpc': '2.0', 'id': msg['id'], 'result': on_request(msg)})
            elif 'method' in msg:
                self.notes.append(msg)

    def handshake(self, protocol='2025-06-18', client='test'):
        r = self.request('initialize', {'protocolVersion': protocol, 'capabilities': {'sampling': {}},
                                        'clientInfo': {'name': client, 'version': '1'}})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return r

    def call(self, tool, timeout=10, on_request=None, **args):
        return self.request('tools/call', {'name': tool, 'arguments': args}, timeout, on_request)

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.p.stdout.close()


def cli(home, *args):
    r = subprocess.run([sys.executable, '-I', LAZY, *args], env=env_for(home), capture_output=True,
                       text=True, timeout=30)
    return r.stdout + r.stderr


def spawns(marker):
    return sorted(int(os.path.basename(p)[6:]) for p in glob.glob(os.path.join(marker, 'spawn-*')))


def wait_until(cond, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def text(resp):
    return resp['result']['content'][0]['text']
