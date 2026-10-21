"""Minimal stdio MCP server for tests and benchmarks.

usage: fake_server.py MARKER_DIR [--alloc MB]
Writes MARKER_DIR/spawn-<pid> on start. Tool names come from MARKER_DIR/tools.txt (default: echo).
"""
import json
import os
import sys
import threading
import time

marker = sys.argv[1]
alloc = int(sys.argv[sys.argv.index('--alloc') + 1]) if '--alloc' in sys.argv else 0
os.makedirs(marker, exist_ok=True)
open(os.path.join(marker, 'spawn-%d' % os.getpid()), 'w').close()

# Hold memory like a real server would
ballast = bytearray(alloc * 2**20)
for i in range(0, len(ballast), 4096):
    ballast[i] = 1

out_lock = threading.Lock()
waiting = {}


def send(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + '\n')
        sys.stdout.flush()


def tool_names():
    try:
        with open(os.path.join(marker, 'tools.txt')) as f:
            return [t.strip() for t in f.read().split(',') if t.strip()]
    except OSError:
        return ['echo']


def call(mid, params):
    name, args = params.get('name'), params.get('arguments') or {}
    text = None
    if name == 'echo':
        text = str(args.get('text', ''))
    elif name == 'pid':
        text = str(os.getpid())
    elif name == 'sleep':
        time.sleep(float(args.get('seconds', 1)))
        text = 'slept'
    elif name == 'chatty':
        def chatter(end):
            while time.time() < end:
                send({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'level': 'info', 'data': 'tick'}})
                time.sleep(0.1)
        threading.Thread(target=chatter, args=(time.time() + float(args.get('seconds', 5)),), daemon=True).start()
        text = 'chatting'
    elif name == 'crash':
        os._exit(3)
    elif name == 'ask':
        ev = threading.Event()
        waiting['srv-1'] = [ev, None]
        send({'jsonrpc': '2.0', 'id': 'srv-1', 'method': 'sampling/createMessage',
              'params': {'messages': [], 'maxTokens': 1}})
        ev.wait(10)
        text = 'client said ' + json.dumps((waiting['srv-1'][1] or {}).get('result'))
    if text is None:
        send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'unknown tool'}})
    else:
        send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': text}]}})


for raw in sys.stdin:
    msg = json.loads(raw)
    method, mid = msg.get('method'), msg.get('id')
    if method is None:
        if mid in waiting:
            waiting[mid][1] = msg
            waiting[mid][0].set()
        continue
    if mid is None:
        continue
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': mid, 'result': {
            'protocolVersion': msg['params'].get('protocolVersion'),
            'capabilities': {'tools': {'listChanged': True}, 'logging': {}},
            'serverInfo': {'name': 'fake', 'version': '1'}}})
    elif method == 'tools/list':
        send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [
            {'name': t, 'description': t, 'inputSchema': {'type': 'object'}} for t in tool_names()]}})
    elif method == 'tools/call':
        threading.Thread(target=call, args=(mid, msg.get('params') or {}), daemon=True).start()
    elif method == 'logging/setLevel':
        with open(os.path.join(marker, 'level-%d' % os.getpid()), 'w') as f:
            f.write(msg['params'].get('level', ''))
        send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
    elif method == 'ping':
        send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
    else:
        send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'Method not found'}})
