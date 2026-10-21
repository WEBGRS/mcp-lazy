import io
import json
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply_config as ac  # noqa: E402

CONFIG = r'''model = "gpt"
notify = [ "C:\\x\\notify.exe", "turn-ended" ]
[desktop]
sansFontSize = 14

[mcp_servers.node_repl]
args = []
command = 'C:\Users\me\node_repl.exe'
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
CODEX_HOME = 'C:\Users\me\.codex'

# web scraping
[mcp_servers.firecrawl]
command = "cmd"
args = [
    "/c",
    "npx",
    "-y",
    "firecrawl-mcp", # pinned later
    'x]y # z',
]

[mcp_servers.firecrawl.env]
FIRECRAWL_API_KEY = "fc-test"

[mcp_servers.playwright]
command = "cmd"
args = ["/c", "npx", "-y", "@playwright/mcp@latest", "--extension", "x]# not a comment"]

[mcp_servers.playwright.env]
PLAYWRIGHT_MCP_EXTENSION_TOKEN = "tok"

[mcp_servers.windows-mcp]
command = "uvx"
args = [
    "windows-mcp",
    "serve",
]

[mcp_servers.docs]
url = "https://example.com/mcp"

[mcp_servers.rea]
command = 'C:\Program Files\nodejs\node.exe'
args = [
    'C:\Users\me\rea.mjs',
    "mcp",
]
startup_timeout_sec = 30

[projects.'c:\users\me\site']
trust_level = "trusted"
'''

STDIO = ('node_repl', 'firecrawl', 'playwright', 'windows-mcp', 'rea')


def argv_of(entry):
    return [entry['command']] + list(entry.get('args') or [])


class ApplyCodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lazy-apply-test-')
        self.cfg = os.path.join(self.tmp, 'config.toml')
        self.orig = os.path.join(self.tmp, 'original-codex-mcp.json')
        self.key = os.path.join(self.tmp, 'key.txt')
        with open(self.key, 'w') as f:
            f.write('k3y\n')
        self.local = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        with open(self.cfg, 'w', encoding='utf-8', newline='') as f:
            f.write(text)

    def read(self):
        with open(self.cfg, encoding='utf-8', newline='') as f:
            return f.read()

    def run_codex(self, action, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            ac.codex(action, self.cfg, self.orig, local=self.local, do_warm=False, **kw)
        return out.getvalue()

    def test_apply_wraps_every_stdio_server_and_nothing_else(self):
        self.write(CONFIG)
        before = tomllib.loads(CONFIG)
        self.assertIn('applied', self.run_codex('apply'))
        after = tomllib.loads(self.read())
        s, b = after['mcp_servers'], before['mcp_servers']
        for name in STDIO:
            self.assertEqual(s[name]['command'], ac.PY)
            self.assertEqual(s[name]['args'], ac.lazy_args(name, argv_of(b[name])))
        self.assertEqual(s['docs'], b['docs'])
        self.assertEqual(s['firecrawl']['env'], {'FIRECRAWL_API_KEY': 'fc-test'})
        self.assertEqual(s['rea']['startup_timeout_sec'], 30)
        self.assertEqual(s['node_repl']['env'], b['node_repl']['env'])
        for key in ('model', 'notify', 'desktop', 'projects'):
            self.assertEqual(after[key], before[key])
        self.assertIn('# web scraping\n[mcp_servers.firecrawl]\ncommand = ', self.read())

    def test_skip_only_and_command_override(self):
        self.write(CONFIG)
        self.local = {'skip': ['node_repl'], 'commands': {'play*': ['node', 'cli.js', '--extension']}}
        self.run_codex('apply', only=['p*', 'node_repl', 'rea'])
        s = tomllib.loads(self.read())['mcp_servers']
        self.assertEqual(s['playwright']['args'], ac.lazy_args('playwright', ['node', 'cli.js', '--extension']))
        self.assertEqual(s['rea']['command'], ac.PY)
        self.assertEqual(s['node_repl']['command'], 'C:\\Users\\me\\node_repl.exe')
        self.assertEqual(s['firecrawl']['command'], 'cmd')

    def test_http_override(self):
        self.write(CONFIG)
        self.local = {'codex_http': {'windows-mcp': {'url': 'http://127.0.0.1:9/mcp', 'bearer_file': self.key}}}
        self.run_codex('apply')
        s = tomllib.loads(self.read())['mcp_servers']
        self.assertEqual(s['windows-mcp'], {'url': 'http://127.0.0.1:9/mcp',
                                            'http_headers': {'Authorization': 'Bearer k3y'}})

    def test_apply_twice_is_a_no_op(self):
        self.write(CONFIG)
        self.run_codex('apply')
        once = self.read()
        self.assertIn('nothing to apply', self.run_codex('apply'))
        self.assertEqual(self.read(), once)

    def test_rollback_restores_the_exact_text(self):
        for text in (CONFIG, CONFIG.replace('\n', '\r\n')):
            self.write(text)
            self.run_codex('apply')
            self.assertNotEqual(self.read(), text)
            self.assertEqual('\r\n' in self.read(), '\r\n' in text)
            self.assertIn('restored', self.run_codex('rollback'))
            self.assertEqual(self.read(), text)
            os.remove(self.orig)

    def test_rollback_survives_other_edits(self):
        self.write(CONFIG)
        self.run_codex('apply')
        self.write(self.read() + '\n[projects.\'c:\\new\']\ntrust_level = "trusted"\n')
        self.run_codex('rollback')
        restored = tomllib.loads(self.read())
        self.assertEqual(restored['mcp_servers'], tomllib.loads(CONFIG)['mcp_servers'])
        self.assertIn('c:\\new', restored['projects'])

    def test_backup_written_before_apply(self):
        self.write(CONFIG)
        self.run_codex('apply')
        backups = [n for n in os.listdir(self.tmp) if n.startswith('codex-config.toml.bak-')]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp, backups[0]), encoding='utf-8', newline='') as f:
            self.assertEqual(f.read(), CONFIG)

    def test_mismatched_edit_is_refused(self):
        with self.assertRaises(SystemExit):
            ac.rewrite_toml(CONFIG, {'firecrawl': ("command = 'a'\n", {'command': 'b'})})

    def test_server_with_env_is_not_switched_to_http(self):
        self.write(CONFIG.replace('[mcp_servers.docs]', '[mcp_servers.windows-mcp.env]\nX = "1"\n\n[mcp_servers.docs]'))
        self.local = {'codex_http': {'windows-mcp': {'url': 'http://127.0.0.1:9/mcp', 'bearer_file': self.key}}}
        out = self.run_codex('apply')
        self.assertIn('skipped windows-mcp', out)
        self.assertEqual(tomllib.loads(self.read())['mcp_servers']['windows-mcp']['command'], 'uvx')


class ApplyClaudeTest(unittest.TestCase):
    SERVERS = {
        'firecrawl': {'type': 'stdio', 'command': 'npx', 'args': ['-y', 'firecrawl-mcp'], 'env': {'FIRECRAWL_API_KEY': 'fc-test'}},
        'playwright-work': {'command': 'npx', 'args': ['@playwright/mcp@latest']},
        'docs': {'type': 'http', 'url': 'https://example.com/mcp'},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lazy-apply-test-')
        self.cfg = os.path.join(self.tmp, '.claude.json')
        self.orig = os.path.join(self.tmp, 'original-mcpServers.json')
        self.doc = {'numStartups': 3, 'mcpServers': json.loads(json.dumps(self.SERVERS)), 'projects': {'a': {}}}
        with open(self.cfg, 'w', encoding='utf-8') as f:
            json.dump(self.doc, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_claude(self, action, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            ac.claude(action, self.cfg, self.orig, local={}, do_warm=False, **kw)
        return out.getvalue()

    def load(self):
        with open(self.cfg, encoding='utf-8') as f:
            return json.load(f)

    def test_apply_and_rollback(self):
        self.assertIn('applied: firecrawl, playwright-work', self.run_claude('apply'))
        d = self.load()
        s = d['mcpServers']
        self.assertEqual(s['firecrawl']['args'], ac.lazy_args('firecrawl', ['npx', '-y', 'firecrawl-mcp']))
        self.assertEqual(s['firecrawl']['env'], {'FIRECRAWL_API_KEY': 'fc-test'})
        self.assertEqual(s['docs'], self.SERVERS['docs'])
        self.assertEqual(d['numStartups'], 3)
        self.assertIn('nothing to apply', self.run_claude('apply'))
        self.assertIn('restored', self.run_claude('rollback'))
        self.assertEqual(self.load(), self.doc)


if __name__ == '__main__':
    unittest.main()
