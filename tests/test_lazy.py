import json
import os
import shutil
import sys
import tempfile
import unittest

from helpers import FAKE, Client, cli, spawns, text, wait_until
from lazy_core import pid_alive, wildcard_match


class WildcardTest(unittest.TestCase):
    def test_matches(self):
        for pattern, name, want in [('playwright*', 'playwright-work', True), ('playwright*', 'playwright', True),
                                    ('*-work', 'playwright-work', True), ('a*b*c', 'abc', True),
                                    ('a*b*c', 'acb', False), ('a*a', 'a', False), ('rea', 'rea', True),
                                    ('rea', 'real', False), ('fire*', 'playwright', False)]:
            self.assertEqual(wildcard_match(pattern, name), want, (pattern, name))


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lazy-mcp-test-')
        self.home = os.path.join(self.tmp, 'home')
        self.marker = os.path.join(self.tmp, 'marker')
        os.makedirs(self.marker)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **kw):
        c = Client(self.home, self.marker, **kw)
        self.clients.append(c)
        return c

    def warm(self, **kw):
        c = self.client(**kw)
        c.handshake()
        c.request('tools/list')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)

    # Cache
    def test_cold_start_spawns_once_and_fills_cache(self):
        c = self.client()
        r = c.handshake()
        self.assertEqual(r['result']['serverInfo']['name'], 'fake')
        self.assertEqual([t['name'] for t in c.request('tools/list')['result']['tools']], ['echo'])
        self.assertEqual(len(spawns(self.marker)), 1)
        self.assertTrue(any(d.startswith('fake-') for d in os.listdir(os.path.join(self.home, 'cache'))))

    def test_warm_session_never_starts_the_server(self):
        self.warm()
        c = self.client()
        self.assertEqual(c.request('server/discover')['error']['code'], -32601)
        self.assertEqual(c.handshake()['result']['serverInfo']['name'], 'fake')
        self.assertEqual(c.request('tools/list')['result']['tools'][0]['name'], 'echo')
        self.assertEqual(c.request('ping')['result'], {})
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)

    def test_cached_reply_keeps_string_request_id(self):
        self.warm()
        c = self.client()
        c.handshake()
        c.send({'jsonrpc': '2.0', 'id': 'abc"1', 'method': 'tools/list', 'params': {}})
        msg = c.recv()
        self.assertEqual(msg['id'], 'abc"1')
        self.assertEqual(msg['result']['tools'][0]['name'], 'echo')

    def test_other_protocol_version_gets_its_own_cache(self):
        self.warm()
        c = self.client()
        c.handshake(protocol='2025-03-26')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 2)

    def test_other_client_gets_its_own_cache(self):
        self.warm()
        c = self.client()
        c.handshake(client='codex-mcp-client')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 2)
        c = self.client()
        c.handshake()
        c.request('tools/list')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 2)

    def test_package_upgrade_invalidates_cache(self):
        pkg = os.path.join(self.tmp, 'pkg')
        os.makedirs(os.path.join(pkg, 'dist'))
        shutil.copy(FAKE, os.path.join(pkg, 'dist', 'server.py'))
        server = [sys.executable, os.path.join(pkg, 'dist', 'server.py'), self.marker]

        def version(v):
            with open(os.path.join(pkg, 'package.json'), 'w') as f:
                json.dump({'name': 'fake-pkg', 'version': v}, f)
        version('1.0.0')
        self.warm(server=server)
        c = self.client(server=server)
        c.handshake()
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)
        version('1.0.1')
        c = self.client(server=server)
        c.handshake()
        c.close()
        self.assertEqual(len(spawns(self.marker)), 2)

    # Lazy start
    def test_first_tool_call_starts_server_once(self):
        self.warm()
        c = self.client()
        c.handshake()
        c.request('tools/list')
        self.assertEqual(text(c.call('echo', text='hi')), 'hi')
        self.assertEqual(text(c.call('echo', text='again')), 'again')
        self.assertEqual(len(spawns(self.marker)), 2)

    def test_initialize_after_server_started_is_answered(self):
        # Regression: a live proxy used to swallow initialize and hang the client
        self.warm()
        c = self.client()
        c.handshake()
        c.call('echo', text='x')
        self.assertIn('result', c.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                                         'clientInfo': {'name': 't', 'version': '1'}}, timeout=3))

    def test_changed_tool_list_notifies_client(self):
        self.warm()
        with open(os.path.join(self.marker, 'tools.txt'), 'w') as f:
            f.write('echo,pid')
        c = self.client()
        c.handshake()
        self.assertEqual(len(c.request('tools/list')['result']['tools']), 1)
        c.call('echo', text='x')
        self.assertTrue(wait_until(lambda: c.request('ping') and any(
            n['method'] == 'notifications/tools/list_changed' for n in c.notes), 5))
        self.assertEqual(len(c.request('tools/list')['result']['tools']), 2)

    def test_list_refreshed_by_another_session_still_notifies(self):
        # A served stale list; B refreshes the shared cache first; A must still hear about it
        self.warm()
        a = self.client()
        a.handshake()
        self.assertEqual(len(a.request('tools/list')['result']['tools']), 1)
        with open(os.path.join(self.marker, 'tools.txt'), 'w') as f:
            f.write('echo,pid')
        b = self.client()
        b.handshake()
        b.request('tools/list')
        b.call('echo', text='x')
        self.assertTrue(wait_until(lambda: b.request('ping') and any(
            n['method'] == 'notifications/tools/list_changed' for n in b.notes), 5))
        a.call('echo', text='y')
        self.assertTrue(wait_until(lambda: a.request('ping') and any(
            n['method'] == 'notifications/tools/list_changed' for n in a.notes), 5))

    def test_log_level_is_replayed_to_server(self):
        self.warm()
        c = self.client()
        c.handshake()
        self.assertEqual(c.request('logging/setLevel', {'level': 'debug'})['result'], {})
        pid = int(text(c.call('pid')))
        with open(os.path.join(self.marker, 'level-%d' % pid)) as f:
            self.assertEqual(f.read(), 'debug')

    def test_server_request_round_trip(self):
        self.warm()
        c = self.client(idle=0.5)
        c.handshake()
        r = c.call('ask', timeout=15, on_request=lambda m: {'role': 'assistant', 'content': 'yes'})
        self.assertIn('assistant', text(r))

    # Idle stop
    def test_idle_server_is_stopped_and_restarted(self):
        self.warm()
        c = self.client(idle=1)
        c.handshake()
        first = int(text(c.call('pid')))
        self.assertTrue(wait_until(lambda: not pid_alive(first), 8), 'server not stopped')
        second = int(text(c.call('pid')))
        self.assertNotEqual(first, second)
        self.assertEqual(text(c.call('echo', text='ok')), 'ok')

    def test_no_stop_while_a_call_is_running(self):
        self.warm()
        c = self.client(idle=0.5)
        c.handshake()
        self.assertEqual(text(c.call('sleep', seconds=2.5, timeout=15)), 'slept')
        self.assertEqual(len(spawns(self.marker)), 2)

    def test_server_notifications_do_not_keep_it_awake(self):
        self.warm()
        c = self.client(idle=1)
        c.handshake()
        pid = int(text(c.call('pid')))
        c.call('chatty', seconds=8)
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 4), 'chatty server kept awake')

    def test_idle_comes_from_settings_pattern(self):
        os.makedirs(self.home)
        with open(os.path.join(self.home, 'settings.json'), 'w') as f:
            json.dump({'idle_seconds': {'other': 600, 'fa*': 1, '*': 600}}, f)
        self.warm()
        c = self.client()
        c.handshake()
        pid = int(text(c.call('pid')))
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 8), 'settings idle not applied')

    # Failures
    def test_server_crash_returns_error_then_recovers(self):
        self.warm()
        c = self.client()
        c.handshake()
        c.call('echo', text='x')
        r = c.call('crash')
        self.assertIn('error', r)
        self.assertEqual(text(c.call('echo', text='back')), 'back')
        self.assertEqual(len(spawns(self.marker)), 3)

    def test_missing_executable_gives_error_and_proxy_survives(self):
        c = self.client(server=[os.path.join(self.tmp, 'nope.exe')])
        r = c.handshake()
        self.assertIn('error', r)
        self.assertEqual(c.request('ping')['result'], {})

    def test_server_that_dies_on_start_gives_error_and_proxy_survives(self):
        c = self.client(server=[sys.executable, os.path.join(self.tmp, 'missing.py')])
        self.assertIn('error', c.handshake())
        self.assertIn('error', c.call('echo', text='x'))
        self.assertEqual(c.request('ping')['result'], {})

    # Process cleanup
    def test_server_stops_when_client_disconnects(self):
        self.warm()
        c = self.client()
        c.handshake()
        pid = int(text(c.call('pid')))
        c.close()
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 8))

    @unittest.skipUnless(os.name == 'nt', 'job object cleanup is Windows-only')
    def test_server_dies_with_hard_killed_proxy(self):
        self.warm()
        c = self.client()
        c.handshake()
        pid = int(text(c.call('pid')))
        c.p.kill()
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 8))

    # --status / --reap
    def states(self):
        rows = []
        for line in cli(self.home, '--status').splitlines()[1:]:
            if not line.strip():
                break
            toks = line.split()
            rows.append(toks[2:4] if line[0] != ' ' else toks[:2])
        return rows

    def test_status_and_reap(self):
        self.warm()
        c = self.client()
        c.handshake()
        self.assertTrue(wait_until(lambda: self.states() == [['fake', 'asleep']], 6))
        pid = int(text(c.call('pid')))
        self.assertTrue(wait_until(lambda: self.states() == [['fake', 'awake']], 3))
        self.assertIn('stopped 1 of 1', cli(self.home, '--reap'))
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.states(), [['fake', 'asleep']])
        self.assertEqual(text(c.call('echo', text='ok')), 'ok')

    def test_awake_state_is_written_without_tick_delay(self):
        self.warm()
        c = self.client()
        c.handshake()
        self.assertTrue(wait_until(lambda: self.states() == [['fake', 'asleep']], 6))
        c.call('pid')
        self.assertIn(self.states(), ([['fake', 'awake']], [['fake', 'busy']]))

    def test_reap_skips_busy_server(self):
        self.warm()
        c = self.client()
        c.handshake()
        c.send({'jsonrpc': '2.0', 'id': 'slow', 'method': 'tools/call',
                'params': {'name': 'sleep', 'arguments': {'seconds': 4}}})
        self.assertTrue(wait_until(lambda: self.states() == [['fake', 'busy']], 6))
        self.assertIn('1 busy, left running', cli(self.home, '--reap'))
        self.assertEqual(text(c.recv(10)), 'slept')

    def test_two_sessions_grouped_in_status(self):
        self.warm()
        a, b = self.client(), self.client(name='other')
        a.handshake()
        b.handshake()
        # 'other' has no cache, so its handshake starts the server
        self.assertTrue(wait_until(lambda: sorted(self.states()) == [['fake', 'asleep'], ['other', 'awake']], 6))
        self.assertIn('1 sessions, 2 proxies', cli(self.home, '--status'))

    def test_status_shows_the_client(self):
        for client, label in (('codex-mcp-client', 'codex'), ('claude-code', 'claude')):
            w = self.client()
            w.handshake(client=client)
            w.request('tools/list')
            w.close()
            # Cache hit: the session stays asleep, so only the handshake can update its state file
            c = self.client()
            c.handshake(client=client)
            self.assertTrue(wait_until(lambda: label in cli(self.home, '--status'), 6))
            self.assertEqual(self.states(), [['fake', 'asleep']])
            c.close()

    def test_warm_fills_cache_from_claude_config(self):
        config = os.path.join(self.tmp, 'claude.json')
        entry = {'type': 'stdio', 'command': sys.executable,
                 'args': ['-I', 'C:\\x\\lazy_mcp.py', 'fake', '--', sys.executable, FAKE, self.marker], 'env': {}}
        with open(config, 'w') as f:
            json.dump({'mcpServers': {'fake': entry, 'plain': {'command': 'node', 'args': ['x.js']}}}, f)
        out = cli(self.home, '--warm', '--config', config, '--protocol', '2025-11-25')
        self.assertIn('ok: 1 tools', out)
        self.assertNotIn('plain', out)
        self.assertEqual(len(spawns(self.marker)), 1)
        c = self.client()
        c.handshake(protocol='2025-11-25', client='claude-code')
        c.request('tools/list')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)

    def codex_config(self, name='fake'):
        config = os.path.join(self.tmp, 'config.toml')
        args = ['-I', 'C:\\x\\lazy_mcp.py', name, '--', sys.executable, FAKE, self.marker]
        with open(config, 'w', encoding='utf-8') as f:
            f.write('model = "x"\n\n[mcp_servers.%s]\ncommand = %s\nargs = %s\n\n'
                    '[mcp_servers.%s.env]\nLAZY_TEST = "1"\n\n[mcp_servers.plain]\ncommand = "node"\n'
                    % (name, json.dumps(sys.executable), json.dumps(args), name))
        return config

    def test_warm_fills_cache_from_codex_config(self):
        out = cli(self.home, '--warm', '--config', self.codex_config())
        self.assertIn('codex', out)
        self.assertIn('ok: 1 tools', out)
        self.assertNotIn('plain', out)
        self.assertEqual(len(spawns(self.marker)), 1)
        c = self.client()
        c.handshake(protocol='2025-06-18', client='codex-mcp-client')
        c.request('tools/list')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)

    def test_warm_replays_the_recorded_client_handshake(self):
        c = self.client(name='recorder')
        c.handshake(protocol='2025-03-26', client='codex-mcp-client')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 1)
        self.assertIn('ok: 1 tools', cli(self.home, '--warm', '--config', self.codex_config()))
        self.assertEqual(len(spawns(self.marker)), 2)
        c = self.client()
        c.handshake(protocol='2025-03-26', client='codex-mcp-client')
        c.request('tools/list')
        c.close()
        self.assertEqual(len(spawns(self.marker)), 2)

    def test_entry_point_runs_core_from_cached_bytecode(self):
        self.warm()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(any(n.startswith('lazy_core.') and n.endswith('.pyc')
                            for n in os.listdir(os.path.join(root, '__pycache__'))))

    def test_stale_state_file_is_pruned(self):
        run = os.path.join(self.home, 'run')
        os.makedirs(run)
        with open(os.path.join(run, 'ghost-999999.json'), 'w') as f:
            json.dump({'name': 'ghost', 'pid': 999999, 'ppid': 1, 'state': 'awake', 'started': 0, 'idle': 1,
                       'last': 0, 'server_pid': None}, f)
        self.assertIn('no lazy-mcp', cli(self.home, '--status'))
        self.assertEqual(os.listdir(run), [])


if __name__ == '__main__':
    unittest.main()
