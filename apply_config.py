"""Wrap the MCP servers in your Claude Code or Codex config with mcp-lazy, or put them back.

usage: python apply_config.py [claude|codex] [apply|rollback] [--only NAME,...] [--skip NAME,...]
       (default: claude apply; NAME may be a pattern such as playwright*)

apply     Every stdio server in the config (one with a command, not a url) is changed to run
          `python -I lazy_mcp.py NAME -- <its own command and args>`. env, cwd, timeouts and every
          other key stay as they are. The replaced values are saved next to this script, and the
          caches are warmed so the first session doesn't start each server once.
rollback  Puts the saved values back.

claude    ~/.claude.json (user-level mcpServers)
codex     $CODEX_HOME/config.toml or ~/.codex/config.toml ([mcp_servers.*] tables, edited in place:
          only command, args, url and http_headers change; comments and order stay)

local.json (optional, next to this script, not in git):
  {"skip": ["node_repl"],
   "commands": {"playwright*": ["node", "/path/to/pkgs/node_modules/@playwright/mcp/cli.js"]},
   "codex_http": {"windows-mcp": {"url": "http://127.0.0.1:8000/mcp", "bearer_file": "/path/key.txt"}}}
  skip: never wrap these. commands: run this instead of the server's own command (for example node
  on a pinned install instead of `npx -y pkg@latest`). codex_http: point a Codex server at an HTTP
  instance you already run, instead of wrapping it.
"""
import copy
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser('~')
CFG = os.path.join(HOME, '.claude.json')
CODEX_CFG = os.path.join(os.environ.get('CODEX_HOME') or os.path.join(HOME, '.codex'), 'config.toml')
ORIG = os.path.join(HERE, 'original-mcpServers.json')
ORIG_CODEX = os.path.join(HERE, 'original-codex-mcp.json')
LOCAL = os.path.join(HERE, 'local.json')
LAZY = os.path.join(HERE, 'lazy_mcp.py')
PY = sys.executable
MANAGED = ('command', 'args', 'url', 'http_headers')


def load_local(path=LOCAL):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except OSError:
        return {}


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns or ())


def lazy_args(name, inner):
    return ['-I', LAZY, name, '--'] + list(inner)


def is_lazy(entry):
    return any(str(a).endswith('lazy_mcp.py') for a in entry.get('args') or [])


def is_stdio(entry):
    return bool(entry.get('command')) and not entry.get('url') and entry.get('type', 'stdio') == 'stdio'


def inner_command(name, entry, local):
    # The real server: a local.json override, else the entry's own command and args
    for pattern, argv in (local.get('commands') or {}).items():
        if fnmatch.fnmatchcase(name, pattern):
            return list(argv)
    return [entry['command']] + list(entry.get('args') or [])


def targets(servers, local, only=(), skip=()):
    # Names to wrap: stdio, not wrapped yet, not skipped, and in --only when given
    return [n for n, e in servers.items()
            if isinstance(e, dict) and is_stdio(e) and not is_lazy(e)
            and not matches(n, list(skip) + list(local.get('skip') or ()))
            and (not only or matches(n, only))
            and n not in (local.get('codex_http') or {})]


def replace_file(path, data):
    tmp = path + '.lazy.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(data)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    raise SystemExit('could not replace ' + path)


def backup(path, prefix, folder=HERE):
    shutil.copy2(path, os.path.join(folder, prefix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))


def warm(path):
    subprocess.run([PY, '-I', LAZY, '--warm', '--config', path])


# Claude Code: ~/.claude.json
def claude(action, path=CFG, orig_path=ORIG, only=(), skip=(), local=None, do_warm=True):
    local = load_local() if local is None else local
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    servers = d.setdefault('mcpServers', {})
    try:
        with open(orig_path, encoding='utf-8') as f:
            orig = json.load(f)
    except OSError:
        orig = {}
    if action == 'rollback':
        names = [n for n in orig if n in servers and (not only or matches(n, only))]
        if not names:
            print('nothing to rollback in %s' % path)
            return
        for n in names:
            servers[n] = orig[n]
        replace_file(path, json.dumps(d, ensure_ascii=False, indent=2))
        print('restored:', ', '.join(names))
        return
    names = targets(servers, local, only, skip)
    if not names:
        print('nothing to apply in %s' % path)
        return
    backup(path, 'claude.json', os.path.dirname(orig_path))
    for n in names:
        e = servers[n]
        orig.setdefault(n, copy.deepcopy(e))
        inner = inner_command(n, e, local)
        e['command'] = PY
        e['args'] = lazy_args(n, inner)
    with open(orig_path, 'w', encoding='utf-8') as f:
        json.dump(orig, f, ensure_ascii=False, indent=2)
    replace_file(path, json.dumps(d, ensure_ascii=False, indent=2))
    print('applied:', ', '.join(names))
    print('rollback: python -I "%s" claude rollback' % os.path.join(HERE, 'apply_config.py'))
    if do_warm:
        warm(path)
    print('restart Claude Code sessions to load it')


# TOML text editing: only key/value statements inside one table change
def statement_end(lines, i):
    # Index after the last line of the key/value statement that starts at lines[i]
    depth, j = 0, i
    while j < len(lines):
        s, k, quote = lines[j], 0, None
        if j == i:
            k = s.index('=') + 1
        while k < len(s):
            ch = s[k]
            if quote:
                if quote == '"' and ch == '\\':
                    k += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '#':
                break
            elif ch in '[{':
                depth += 1
            elif ch in ']}':
                depth -= 1
            k += 1
        j += 1
        if depth <= 0:
            return j
    return j


def blocks(lines):
    # (kind, start, end, key or header) for each table header and key/value statement
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith('#'):
            i += 1
        elif s.startswith('['):
            out.append(('table', i, i + 1, s.split('#')[0].strip()))
            i += 1
        else:
            end = statement_end(lines, i)
            out.append(('kv', i, end, s.split('=', 1)[0].strip().strip('"\'')))
            i = end
    return out


def edit_table(lines, name, new_text):
    """Swap the MANAGED statements of [mcp_servers.NAME] for new_text; return (lines, removed text)."""
    heads = {'[mcp_servers.%s]' % name, '[mcp_servers."%s"]' % name, "[mcp_servers.'%s']" % name}
    inside, spans, header_end = False, [], None
    for kind, start, end, key in blocks(lines):
        if kind == 'table':
            if inside:
                break
            if key in heads:
                inside, header_end = True, end
        elif inside and key in MANAGED:
            spans.append((start, end))
    if header_end is None:
        raise SystemExit('no [mcp_servers.%s] table' % name)
    removed = ''.join(''.join(lines[a:b]) for a, b in spans)
    at = spans[0][0] if spans else header_end  # nothing before it is removed
    keep = [ln for idx, ln in enumerate(lines) if not any(a <= idx < b for a, b in spans)]
    keep[at:at] = new_text.splitlines(True)
    return keep, removed


def toml_value(v):
    if isinstance(v, str):
        return "'%s'" % v if "'" not in v and '\n' not in v else json.dumps(v)
    if isinstance(v, dict):
        return '{ %s }' % ', '.join('%s = %s' % (k, toml_value(x)) for k, x in v.items())
    raise TypeError(v)


def render(values, nl):
    out = []
    for k, v in values.items():
        if isinstance(v, list):
            out.append('%s = [%s%s]%s' % (k, nl, ''.join('    %s,%s' % (toml_value(a), nl) for a in v), nl))
        else:
            out.append('%s = %s%s' % (k, toml_value(v), nl))
    return ''.join(out)


def rewrite_toml(text, changes):
    """changes: {name: (statements text, {key: value} it sets)}. Returns (new text, {name: removed text}).

    Refuses unless the parsed result equals the old one with only the MANAGED keys of those tables changed.
    """
    import tomllib
    data = tomllib.loads(text)
    lines = text.splitlines(True)
    removed = {}
    for name, (stmts, _) in changes.items():
        lines, removed[name] = edit_table(lines, name, stmts)
    new = ''.join(lines)
    expect = copy.deepcopy(data)
    for name, (_, values) in changes.items():
        e = expect['mcp_servers'][name]
        for k in MANAGED:
            e.pop(k, None)
        e.update(values)
    if tomllib.loads(new) != expect:
        raise SystemExit('refusing to write: the edited config would differ in more than the target keys')
    return new, removed


# Codex: config.toml
def codex(action, path=CODEX_CFG, orig_path=ORIG_CODEX, only=(), skip=(), local=None, do_warm=True):
    import tomllib
    local = load_local() if local is None else local
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    nl = '\r\n' if '\r\n' in text else '\n'
    servers = tomllib.loads(text).get('mcp_servers') or {}
    try:
        with open(orig_path, encoding='utf-8') as f:
            orig = json.load(f)
    except OSError:
        orig = {}
    changes = {}
    if action == 'rollback':
        for name, stmts in orig.items():
            if name in servers and (not only or matches(name, only)):
                changes[name] = (stmts, tomllib.loads(stmts))
    else:
        for name in targets(servers, local, only, skip):
            values = {'command': PY, 'args': lazy_args(name, inner_command(name, servers[name], local))}
            changes[name] = (render(values, nl), values)
        for name, http in (local.get('codex_http') or {}).items():
            e = servers.get(name)
            if e is None or e.get('url') or (only and not matches(name, only)):
                continue
            key_file = http.get('bearer_file')
            if e.get('env') or e.get('env_vars') or e.get('cwd') or (key_file and not os.path.exists(key_file)):
                print('skipped %s: has env/cwd or no bearer file' % name)
                continue
            values = {'url': http['url']}
            if key_file:
                with open(key_file, encoding='utf-8') as f:
                    values['http_headers'] = {'Authorization': 'Bearer ' + f.read().strip()}
            changes[name] = (render(values, nl), values)
    if not changes:
        print('nothing to %s in %s' % (action, path))
        return
    new, removed = rewrite_toml(text, changes)
    backup(path, 'codex-config.toml', os.path.dirname(orig_path))
    if action != 'rollback':
        for name, stmts in removed.items():
            orig.setdefault(name, stmts)
        with open(orig_path, 'w', encoding='utf-8') as f:
            json.dump(orig, f, ensure_ascii=False, indent=2)
    replace_file(path, new)
    print('%s: %s' % ('restored' if action == 'rollback' else 'applied', ', '.join(changes)))
    if action != 'rollback':
        print('rollback: python -I "%s" codex rollback' % os.path.join(HERE, 'apply_config.py'))
        if do_warm:
            warm(path)
    print('restart Codex (desktop app and IDE extension) to load it')


def main(argv):
    target = argv.pop(0) if argv and argv[0] in ('claude', 'codex') else 'claude'
    action = argv.pop(0) if argv and argv[0] in ('apply', 'rollback') else 'apply'
    opts = {'only': [], 'skip': []}
    while argv:
        flag = argv.pop(0)
        if flag not in ('--only', '--skip') or not argv:
            raise SystemExit(__doc__)
        opts[flag[2:]] += [x for x in argv.pop(0).split(',') if x]
    (codex if target == 'codex' else claude)(action, only=opts['only'], skip=opts['skip'])


if __name__ == '__main__':
    main(sys.argv[1:])
