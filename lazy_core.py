"""Lazy stdio MCP proxy.

usage:
  python lazy_mcp.py NAME [--idle SECONDS] -- COMMAND [ARGS...]
  python lazy_mcp.py --status    proxies, awake servers and their memory
  python lazy_mcp.py --reap      stop every awake server that has nothing in flight
  python lazy_mcp.py --warm [NAME...] [--protocol P] [--config PATH]...
                                 fill the cache for the lazy_mcp entries in ~/.claude.json and
                                 ~/.codex/config.toml

Answers the handshake and list requests from cache, starts the real server on the first other
request and stops it again after the idle timeout from settings.json. Works with any stdio MCP
client; caches are kept per client (clientInfo.name) and protocol version.
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

VERSION = 3
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get('LAZY_MCP_HOME') or HERE
CACHE_DIR = os.path.join(ROOT, 'cache')
CLIENT_DIR = os.path.join(CACHE_DIR, 'clients')
LOG_DIR = os.path.join(ROOT, 'logs')
RUN_DIR = os.path.join(ROOT, 'run')
SETTINGS = os.path.join(ROOT, 'settings.json')
REAP_FLAG = os.path.join(RUN_DIR, 'reap-request')

CACHEABLE = {'tools/list': 'tools', 'prompts/list': 'prompts',
             'resources/list': 'resources', 'resources/templates/list': 'resources'}
CHANGED = {'tools/list': 'notifications/tools/list_changed',
           'prompts/list': 'notifications/prompts/list_changed',
           'resources/list': 'notifications/resources/list_changed',
           'resources/templates/list': 'notifications/resources/list_changed'}
DEFAULT_PROTOCOL = '2025-06-18'
DEFAULTS = {'idle_seconds': {'*': 600}, 'max_cache_age_days': 7, 'start_timeout_seconds': 120}
# Handshakes --warm uses until a real client of that kind has connected
KNOWN_CLIENTS = {
    'claude': {'protocolVersion': '2025-11-25', 'capabilities': {'roots': {'listChanged': True}},
               'clientInfo': {'name': 'claude-code', 'version': '0'}},
    'codex': {'protocolVersion': '2025-06-18', 'capabilities': {},
              'clientInfo': {'name': 'codex-mcp-client', 'version': '0'}},
}
CLIENT_LABELS = (('claude', 'claude'), ('codex', 'codex'))


# Helpers
def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(data):
    return hashlib.sha1(data).digest()


def key_of(mid):
    return json.dumps(mid)


def safe(name):
    return ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in name)[:64] or 'server'


def atomic_write(path, data):
    tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'wb') as f:
            f.write(data)
        for _ in range(20):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.05)
    except OSError:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def wildcard_match(pattern, name):
    # '*' only; fnmatch would pull in re (+1 MB per proxy)
    parts = pattern.split('*')
    if len(parts) == 1:
        return pattern == name
    head, tail = parts[0], parts[-1]
    if len(name) < len(head) + len(tail) or not name.startswith(head) or not name.endswith(tail):
        return False
    pos, end = len(head), len(name) - len(tail)
    for part in parts[1:-1]:
        i = name.find(part, pos, end)
        if i < 0:
            return False
        pos = i + len(part)
    return True


def idle_for(name, table):
    # Exact name, then the first matching pattern, then '*'
    if name in table:
        return float(table[name])
    for pattern, value in table.items():
        if pattern != '*' and wildcard_match(pattern, name):
            return float(value)
    return float(table.get('*', 600))


def package_versions(argv):
    # name@version of the npm package behind each script argument
    out = []
    for a in argv:
        if not os.path.isfile(a):
            continue
        d = os.path.dirname(os.path.abspath(a))
        for _ in range(5):
            try:
                with open(os.path.join(d, 'package.json'), encoding='utf-8') as f:
                    pkg = json.load(f)
                if isinstance(pkg, dict) and pkg.get('version'):
                    out.append('%s@%s' % (pkg.get('name'), pkg['version']))
                    break
            except (OSError, ValueError):
                pass
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return out


def client_name(params):
    info = params.get('clientInfo')
    return str(info.get('name') or '') if isinstance(info, dict) else ''


def client_label(name):
    low = name.lower()
    for needle, label in CLIENT_LABELS:
        if needle in low:
            return label
    return name[:8] or '?'


def remember_client(params):
    # --warm replays the handshake real clients send
    data = dumps(params)
    path = os.path.join(CLIENT_DIR, safe(client_name(params) or 'unknown') + '.json')
    try:
        with open(path, 'rb') as f:
            if f.read() == data:
                return
    except OSError:
        pass
    atomic_write(path, data)


def recorded_client(kind):
    # Newest recorded handshake from a client whose name contains `kind`, else the built-in one
    best, best_time = None, -1.0
    try:
        names = os.listdir(CLIENT_DIR)
    except OSError:
        names = []
    for n in names:
        path = os.path.join(CLIENT_DIR, n)
        if kind in n.lower() and n.endswith('.json') and os.path.getmtime(path) > best_time:
            best, best_time = path, os.path.getmtime(path)
    if best:
        try:
            with open(best, encoding='utf-8') as f:
                params = json.load(f)
            if isinstance(params, dict) and client_name(params):
                return params
        except (OSError, ValueError):
            pass
    return json.loads(json.dumps(KNOWN_CLIENTS.get(kind) or KNOWN_CLIENTS['claude']))


def fmt_secs(s):
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm' % (s // 60)
    return '%.1fh' % (s / 3600.0)


class Log:
    def __init__(self, name):
        self.path = os.path.join(LOG_DIR, safe(name) + '.log')
        self.lock = threading.Lock()

    def __call__(self, event):
        with self.lock:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > 256 * 1024:
                    os.replace(self.path, self.path + '.1')
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write('%s pid=%d %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), os.getpid(), event))
            except OSError:
                pass


# Processes: the server tree lives in a kill-on-close job object
if os.name == 'nt':
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ntdll = ctypes.WinDLL('ntdll')
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class _Basic(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('a', 'b', 'c', 'd', 'e', 'f')]

    class _Ext(ctypes.Structure):
        _fields_ = [('Basic', _Basic), ('Io', _Io), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _Mem(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
            'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage',
            'PrivateUsage')]

    class _Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]

    def _make_job():
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Ext()
        info.Basic.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        return job


def spawn_tree(argv):
    import shutil
    argv = [shutil.which(argv[0]) or argv[0]] + list(argv[1:])
    if os.name != 'nt':
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
        p.job = None
        return p
    # Suspended until it is inside the job, so no grandchild escapes
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         creationflags=0x08000000 | 0x00000004)  # CREATE_NO_WINDOW | CREATE_SUSPENDED
    job = _make_job()
    if job and not k32.AssignProcessToJobObject(job, int(p._handle)):
        k32.CloseHandle(job)
        job = None
    p.job = job
    ntdll.NtResumeProcess(int(p._handle))
    return p


def close_job(p):
    job = getattr(p, 'job', None)
    if job:
        p.job = None
        k32.TerminateJobObject(job, 1)
        k32.CloseHandle(job)


def kill_tree(p, grace=2.0):
    # Close stdin, give the server `grace` seconds, then kill the whole tree
    try:
        p.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        p.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    if os.name == 'nt':
        close_job(p)
    else:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass
    try:
        p.wait(2)
    except subprocess.TimeoutExpired:
        pass


def pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def private_mb(pid):
    if os.name != 'nt' or not pid:
        return 0.0
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
    if not h:
        return 0.0
    try:
        m = _Mem()
        m.cb = ctypes.sizeof(m)
        if not k32.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            return 0.0
        return m.PrivateUsage / 2.0 ** 20
    finally:
        k32.CloseHandle(h)


def process_parents():
    # pid -> parent pid for every process
    parents = {}
    if os.name != 'nt':
        return parents
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return parents
    try:
        e = _Entry()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            parents[e.th32ProcessID] = e.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return parents


def tree_of(pid, parents):
    kids = {}
    for child, parent in parents.items():
        kids.setdefault(parent, []).append(child)
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


# Cache: one directory per (argv, protocol, package versions, client), one raw JSON file per method
class Cache:
    def __init__(self, name, argv, max_age):
        self.name, self.argv, self.max_age = name, argv, max_age
        self.dir = None
        self.fresh = False
        self.lock = threading.Lock()

    def load(self, protocol, client):
        key = json.dumps([self.argv, protocol, package_versions(self.argv), client])
        self.dir = os.path.join(CACHE_DIR, '%s-%s' % (safe(self.name),
                                                      hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]))
        try:
            self.fresh = time.time() - os.path.getmtime(self.path('initialize')) <= self.max_age
        except OSError:
            self.fresh = False

    def path(self, method):
        return os.path.join(self.dir, method.replace('/', '-') + '.json')

    def get(self, method):
        if self.dir is None or not self.fresh:
            return None
        try:
            with open(self.path(method), 'rb') as f:
                return f.read() or None
        except OSError:
            return None

    def put(self, method, data):
        if self.dir is None:
            return
        path = self.path(method)
        with self.lock:
            try:
                with open(path, 'rb') as f:
                    same = f.read() == data
            except OSError:
                same = False
            if same:
                try:
                    os.utime(path)
                except OSError:
                    pass
            else:
                atomic_write(path, data)
            if method == 'initialize':
                self.fresh = True


class Child:
    """One running instance of the real server."""

    def __init__(self, argv, on_line, on_exit):
        self.p = spawn_tree(argv)
        self.ready = False
        self.reaped = False
        self.internal = {}  # proxy-owned request id -> [Event, response]
        self.wlock = threading.Lock()
        self.on_line, self.on_exit = on_line, on_exit
        threading.Thread(target=self._read, daemon=True).start()

    def write(self, line):
        try:
            with self.wlock:
                self.p.stdin.write(line + b'\n')
                self.p.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _read(self):
        for raw in self.p.stdout:
            line = raw.strip()
            if line:
                self.on_line(self, line)
        code = self.p.wait()
        for slot in list(self.internal.values()):
            slot[0].set()
        self.on_exit(self, code)


class Proxy:
    def __init__(self, name, argv, idle, max_age, start_timeout):
        self.name, self.argv, self.idle, self.start_timeout = name, argv, idle, start_timeout
        self.cache = Cache(name, argv, max_age)
        self.log = Log(name)
        self.lock = threading.RLock()
        self.out_lock = threading.Lock()
        self.child = None
        self.client_init = None
        self.client = ''
        self.init_raw = None
        self.level = None
        self.pending = {}            # client request id -> (method, record result in cache)
        self.server_pending = set()  # server-initiated request ids awaiting the client
        self.pinned = False          # resources/subscribe seen: never stop
        self.served = {}             # list method -> digest of what the client was given
        self.last = time.monotonic()
        self.seq = 0
        self.stats = {'spawns': 0, 'stops': 0, 'crashes': 0}
        self.started = time.time()
        self.tick = min(1.0, max(0.05, idle / 4.0)) if idle > 0 else 1.0
        self.state_path = os.path.join(RUN_DIR, '%s-%d.json' % (safe(name), os.getpid()))
        self.state_lock = threading.Lock()
        self.state_key = None
        self.state_time = 0.0
        try:
            self.reap_seen = max(self.started, os.path.getmtime(REAP_FLAG))
        except OSError:
            self.reap_seen = self.started

    # Client output
    def send_raw(self, line):
        try:
            with self.out_lock:
                sys.stdout.buffer.write(line + b'\n')
                sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass

    def send(self, msg):
        self.send_raw(dumps(msg))

    def reply(self, mid, result):
        self.send({'jsonrpc': '2.0', 'id': mid, 'result': result})

    def reply_raw(self, mid, raw):
        self.send_raw(b'{"jsonrpc":"2.0","id":' + dumps(mid) + b',"result":' + raw + b'}')

    def error(self, mid, code, message):
        self.send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}})

    # Main loop
    def run(self):
        self.log('start v%d idle=%s ppid=%d' % (VERSION, ('%gs' % self.idle) if self.idle > 0 else 'never',
                                                os.getppid()))
        self.write_state()
        threading.Thread(target=self.reaper, daemon=True).start()
        for raw in sys.stdin.buffer:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.handle(line, msg)
            else:
                self.forward(line, None, 'batch')
        self.shutdown()

    def handle(self, line, msg):
        method = msg.get('method')
        mid = msg.get('id')
        params = msg.get('params')
        if not isinstance(params, dict):
            params = {}
        if method is None:
            # Client's answer to a server-initiated request
            self.forward(line, msg, None)
            return
        if 'id' not in msg:
            if method != 'notifications/initialized':
                self.forward(line, msg, None)
            return
        if method == 'initialize':
            self.client_init = params
            self.client = client_name(params)
            protocol = params.get('protocolVersion') or DEFAULT_PROTOCOL
            self.cache.load(protocol, self.client)
            remember_client(params)
            raw = self.cache.get('initialize')
            if raw is None and self.running():
                raw = self.init_raw
            if raw is None:
                self.log('no cache for %s, protocol %s' % (self.client or 'unnamed client', protocol))
                if self.start('no cache') is not None:
                    raw = self.init_raw
            if raw is None:
                self.error(mid, -32603, 'lazy-mcp: could not start the MCP server (see %s)' % self.log.path)
            else:
                self.reply_raw(mid, raw)
            self.write_state()
            return
        if method == 'ping':
            self.reply(mid, {})
            return
        if method == 'server/discover':
            # Initialize-only; clients that try discovery first fall back to initialize
            self.error(mid, -32601, 'Method not found')
            return
        if method == 'logging/setLevel':
            self.level = params
            if not self.running():
                self.reply(mid, {})
                return
        if method in CACHEABLE and not params.get('cursor') and not self.running():
            raw = self.cache.get(method)
            if raw is not None:
                self.served[method] = digest(raw)
                self.reply_raw(mid, raw)
                return
        if method == 'resources/subscribe':
            self.pinned = True
        self.forward(line, msg, method + (':%s' % params.get('name') if method == 'tools/call' else ''))

    def running(self):
        with self.lock:
            return self.child is not None and self.child.ready

    def forward(self, line, msg, spawn_reason):
        """Send to the running server; start it first if spawn_reason is set."""
        for attempt in range(3):
            with self.lock:
                c = self.child
                if c is not None and c.ready:
                    self.track(msg)
                    self.last = time.monotonic()
                    c.write(line)
                    return
            if spawn_reason is None:
                return
            if attempt == 2 or self.start(spawn_reason) is None:
                if msg is not None and 'id' in msg:
                    self.error(msg['id'], -32603,
                               'lazy-mcp: could not start the MCP server (see %s)' % self.log.path)
                return

    def track(self, msg):
        if msg is None:
            return
        method, mid = msg.get('method'), msg.get('id')
        if method is None and mid is not None:
            self.server_pending.discard(key_of(mid))
        elif method == 'notifications/cancelled':
            self.pending.pop(key_of((msg.get('params') or {}).get('requestId')), None)
        elif method is not None and mid is not None:
            record = method in CACHEABLE and not (msg.get('params') or {}).get('cursor')
            self.pending[key_of(mid)] = (method, record)

    # Real server lifecycle
    def start(self, reason):
        self.log('spawn (%s)' % reason)
        try:
            c = Child(self.argv, self.on_child_line, self.on_child_exit)
        except OSError as e:
            self.log('spawn failed: %s' % e)
            return None
        with self.lock:
            self.child = c
            self.stats['spawns'] += 1
        params = self.client_init or {'protocolVersion': DEFAULT_PROTOCOL, 'capabilities': {},
                                      'clientInfo': {'name': 'lazy-mcp', 'version': str(VERSION)}}
        r = self.request(c, 'initialize', params, self.start_timeout)
        if not r or not isinstance(r.get('result'), dict):
            self.log('initialize failed: %s' % (json.dumps(r)[:300] if r else 'no reply'))
            self.drop(c)
            return None
        c.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
        if self.level is not None:
            self.request(c, 'logging/setLevel', self.level, 30)
        self.init_raw = dumps(r['result'])
        self.cache.put('initialize', self.init_raw)
        with self.lock:
            c.ready = True
            self.last = time.monotonic()
        self.write_state()
        caps = r['result'].get('capabilities') or {}
        threading.Thread(target=self.refresh, args=(c, caps), daemon=True).start()
        return r['result']

    def request(self, c, method, params, timeout):
        with self.lock:
            self.seq += 1
            mid = 'lazy-%d' % self.seq
        slot = [threading.Event(), None]
        c.internal[mid] = slot
        line = json.dumps({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params}).encode('utf-8')
        if not c.write(line) or not slot[0].wait(timeout):
            c.internal.pop(mid, None)
            return None
        c.internal.pop(mid, None)
        return slot[1]

    def refresh(self, c, caps):
        # Re-read the lists; tell the client if what it was given from cache is out of date
        for method, cap in CACHEABLE.items():
            if cap not in caps:
                continue
            r = self.request(c, method, {}, 60)
            if not r or not isinstance(r.get('result'), dict) or r['result'].get('nextCursor'):
                continue
            data = dumps(r['result'])
            self.cache.put(method, data)
            old = self.served.get(method)
            if old is not None and old != digest(data):
                self.served[method] = digest(data)
                self.log('%s changed; notifying client' % method)
                self.send({'jsonrpc': '2.0', 'method': CHANGED[method]})

    def on_child_line(self, c, line):
        try:
            msg = json.loads(line)
        except ValueError:
            self.log('non-JSON line from server dropped')
            return
        record = None
        if isinstance(msg, dict):
            method, mid = msg.get('method'), msg.get('id')
            if method is None and isinstance(mid, str) and mid in c.internal:
                slot = c.internal[mid]
                slot[1] = msg
                slot[0].set()
                return
            with self.lock:
                if method is None and mid is not None:
                    self.last = time.monotonic()
                    sent = self.pending.pop(key_of(mid), None)
                    if sent and sent[1] and isinstance(msg.get('result'), dict) \
                            and not msg['result'].get('nextCursor'):
                        record = sent[0]
                elif method is not None and mid is not None:
                    self.last = time.monotonic()
                    self.server_pending.add(key_of(mid))
        if record:
            data = dumps(msg['result'])
            self.cache.put(record, data)
            self.served[record] = digest(data)
        self.send_raw(line)

    def on_child_exit(self, c, code):
        with self.lock:
            if self.child is not c:
                return
            self.child = None
            failed = [] if c.reaped else list(self.pending)
            self.pending.clear()
            self.server_pending.clear()
            if not c.reaped:
                self.stats['crashes'] += 1
        close_job(c.p)
        self.write_state()
        if not c.reaped:
            self.log('server exited code=%s' % code)
        for k in failed:
            self.error(json.loads(k), -32603,
                       'lazy-mcp: MCP server exited (code %s); the next call restarts it' % code)

    def drop(self, c):
        with self.lock:
            c.reaped = True
            if self.child is c:
                self.child = None
        kill_tree(c.p, grace=0.5)

    def reap_requested(self):
        try:
            m = os.path.getmtime(REAP_FLAG)
        except OSError:
            return False
        if m <= self.reap_seen:
            return False
        self.reap_seen = m
        return True

    def reaper(self):
        while True:
            with self.lock:
                awake = self.child is not None
            time.sleep(self.tick if awake else 2.0)
            force = self.reap_requested()
            victim = why = None
            with self.lock:
                c = self.child
                if c is not None and c.ready and not (self.pending or self.server_pending or self.pinned):
                    due = self.idle > 0 and time.monotonic() - self.last >= self.idle
                    if due or force:
                        c.reaped = True
                        self.child = None
                        self.stats['stops'] += 1
                        victim, why = c, ('%gs idle' % self.idle) if due else 'reap request'
            if victim is not None:
                self.log('stop (%s)' % why)
                kill_tree(victim.p)
            self.write_state()

    def write_state(self):
        # run/NAME-PID.json for --status; rewritten on change, every 10 s while awake
        with self.state_lock:
            with self.lock:
                c = self.child
                state = 'asleep' if c is None else ('awake' if c.ready else 'starting')
                busy = len(self.pending) + len(self.server_pending)
                info = {'name': self.name, 'pid': os.getpid(), 'ppid': os.getppid(), 'version': VERSION,
                        'client': self.client, 'started': self.started, 'idle': self.idle, 'state': state,
                        'busy': busy,
                        'server_pid': c.p.pid if c is not None else None,
                        'last': time.time() - (time.monotonic() - self.last)}
                info.update(self.stats)
            key = (state, info['server_pid'], busy, self.client, tuple(sorted(self.stats.items())))
            now = time.monotonic()
            if key == self.state_key and (state == 'asleep' or now - self.state_time < 10):
                return
            if atomic_write(self.state_path, dumps(info)):
                self.state_key, self.state_time = key, now

    def shutdown(self):
        with self.lock:
            c = self.child
            self.child = None
            if c is not None:
                c.reaped = True
        if c is not None:
            kill_tree(c.p)
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        self.log('stdin closed')
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os._exit(0)


# --status / --reap
def read_states():
    rows = []
    try:
        names = [n for n in os.listdir(RUN_DIR) if n.endswith('.json')]
    except OSError:
        names = []
    for path in (os.path.join(RUN_DIR, n) for n in names):
        try:
            with open(path, encoding='utf-8') as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not pid_alive(info.get('pid', 0)):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        rows.append(info)
    return rows


def status():
    rows = read_states()
    if not rows:
        print('no lazy-mcp v%d proxies running' % VERSION)
        return
    parents = process_parents()
    sessions = {}
    for r in rows:
        sessions.setdefault(r['ppid'], []).append(r)
    now = time.time()
    print('%-8s %-7s %-20s %-8s %-11s %8s %9s %6s' % ('SESSION', 'CLIENT', 'SERVER', 'STATE', 'IDLE/LIMIT',
                                                      'PROXY MB', 'SERVER MB', 'SPAWNS'))
    total_proxy = total_server = awake = 0
    for ppid, group in sorted(sessions.items(), key=lambda kv: min(r['started'] for r in kv[1])):
        client = client_label(next((r['client'] for r in group if r.get('client')), ''))
        for i, r in enumerate(sorted(group, key=lambda r: r['name'])):
            pm = private_mb(r['pid'])
            sm = sum(private_mb(p) for p in tree_of(r['server_pid'], parents)) if r.get('server_pid') else 0.0
            total_proxy += pm
            total_server += sm
            state = r['state']
            idle = ''
            if state == 'awake':
                awake += 1
                if r.get('busy'):
                    state = 'busy'
                limit = fmt_secs(r['idle']) if r['idle'] > 0 else 'never'
                idle = '%s/%s' % (fmt_secs(max(0, now - r['last'])), limit)
            first = i == 0
            print('%-8s %-7s %-20s %-8s %-11s %8.1f %9s %6d' % (
                ppid if first else '', client if first else '', r['name'][:20], state, idle, pm,
                ('%.0f' % sm) if sm else '', r.get('spawns', 0)))
    print('\n%d sessions, %d proxies (%.0f MB); %d servers awake (%.0f MB)'
          % (len(sessions), len(rows), total_proxy, awake, total_server))


def reap():
    # Each proxy checks the flag on its next tick and skips it if a request is in flight
    before = {r['pid'] for r in read_states() if r['state'] in ('awake', 'starting')}
    if not before:
        print('no awake servers')
        return
    atomic_write(REAP_FLAG, str(time.time()).encode())
    deadline = time.monotonic() + 6
    left = before
    while left and time.monotonic() < deadline:
        time.sleep(0.25)
        left = {r['pid'] for r in read_states() if r['pid'] in before and r['state'] != 'asleep'}
    print('stopped %d of %d awake servers%s' % (len(before - left), len(before),
                                                 '; %d busy, left running' % len(left) if left else ''))


def config_servers(path):
    # (client kind, {name: entry}) from ~/.claude.json or a Codex config.toml
    if path.lower().endswith('.toml'):
        import tomllib
        with open(path, 'rb') as f:
            return 'codex', tomllib.load(f).get('mcp_servers') or {}
    with open(path, encoding='utf-8') as f:
        return 'claude', json.load(f).get('mcpServers') or {}


def warm(argv):
    # Fill the cache for every lazy_mcp entry in the client configs
    home = os.path.expanduser('~')
    configs, protocol, only = [], None, []
    while argv:
        a = argv.pop(0)
        if a == '--config' and argv:
            configs.append(argv.pop(0))
        elif a == '--protocol' and argv:
            protocol = argv.pop(0)
        else:
            only.append(a)
    if not configs:
        configs = [p for p in (os.path.join(home, '.claude.json'), os.path.join(home, '.codex', 'config.toml'))
                   if os.path.exists(p)]
    done = 0
    for config in configs:
        kind, servers = config_servers(config)
        hello = recorded_client(kind)
        if protocol:
            hello['protocolVersion'] = protocol
        for name, e in servers.items():
            if warm_one(kind, name, e, hello, only):
                done += 1
    if not done:
        print('no lazy_mcp entries in %s' % ', '.join(configs))


def warm_one(kind, name, e, hello, only):
    args = [str(a) for a in (e.get('args') or [])] if isinstance(e, dict) else []
    if '--' not in args or not any(a.endswith('lazy_mcp.py') for a in args) or (only and name not in only):
        return False
    inner = args[args.index('--') + 1:]
    env = dict(os.environ, **{k: str(v) for k, v in (e.get('env') or {}).items()})
    env['LAZY_MCP_HOME'] = ROOT
    p = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'lazy_mcp.py'), name, '--idle', '0', '--'] + inner,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    watchdog = threading.Timer(180, p.kill)
    watchdog.start()

    def ask(mid, method, params):
        p.stdin.write(dumps({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params}) + b'\n')
        p.stdin.flush()
        for line in p.stdout:
            msg = json.loads(line)
            if msg.get('id') == mid and 'method' not in msg:
                return msg
        return {}
    try:
        r = ask(1, 'initialize', hello)
        caps = (r.get('result') or {}).get('capabilities') or {}
        got = []
        for i, (method, cap) in enumerate(CACHEABLE.items()):
            if cap in caps:
                res = ask(i + 2, method, {}).get('result') or {}
                got.append('%d %s' % (len(next(iter(res.values()), [])) if res else 0, method.split('/')[0]))
        result = ('ok: ' + ', '.join(got)) if 'result' in r else 'failed: %s' % (r.get('error') or 'no reply')
    except (OSError, ValueError) as ex:
        result = 'failed: %s' % ex
    finally:
        watchdog.cancel()
        try:
            p.stdin.close()
            p.wait(30)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()
    print('%-7s %-20s %s' % (kind, name, result))
    return True


def main(argv):
    if argv[:1] == ['--status']:
        return status()
    if argv[:1] == ['--reap']:
        return reap()
    if argv[:1] == ['--warm']:
        return warm(argv[1:])
    if '--' not in argv or argv.index('--') < 1 or argv.index('--') == len(argv) - 1:
        sys.stderr.write(__doc__)
        sys.exit(2)
    i = argv.index('--')
    head, cmd = argv[:i], argv[i + 1:]
    settings = load_settings()
    idle = float(head[head.index('--idle') + 1]) if '--idle' in head[:-1] else \
        idle_for(head[0], settings.get('idle_seconds') or {})
    Proxy(head[0], cmd, idle, float(settings['max_cache_age_days']) * 86400,
          float(settings['start_timeout_seconds'])).run()


if __name__ == '__main__':
    main(sys.argv[1:])
