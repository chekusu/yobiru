"""Opt-in: real Waypost plus the real packaged Yobiru CLI and two HTTP receivers.

Set WAYPOST_TEST_BINARY to a locally built Waypost executable. Only Tailscale's
identity lookup and loopback range are substituted by the existing test harness;
message validation, delivery, storage, bridge, SQL and receipt APIs run for real.
No production credentials, external peers, models or bookings are involved.
"""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer

import test_e2e_loopback as loopback
from support.pkg import Package, SUPPORT, free_port

ROOT = Path(__file__).resolve().parents[1]


class Api:
    def __init__(self, origin, token):
        self.origin, self.token = origin, token

    def call(self, method, path, value=None):
        request = urllib.request.Request(self.origin + path, method=method,
            data=None if value is None else json.dumps(value).encode(),
            headers={'Authorization': 'Bearer ' + self.token,
                     'Content-Type': 'application/json', 'User-Agent': 'Waypost/0.1.0'})
        with urllib.request.urlopen(request, timeout=10) as response:
            envelope = json.load(response)
        if not envelope.get('ok'):
            raise AssertionError('Waypost API failed: ' + envelope['error']['code'])
        return envelope['data']


class BridgeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'posix':
            raise unittest.SkipTest('The optional bridge currently requires POSIX.')
        binary = os.environ.get('WAYPOST_TEST_BINARY')
        if not binary:
            raise unittest.SkipTest('Set WAYPOST_TEST_BINARY for the real Waypost integration test.')
        cls.pkg = Package()
        cls.origin = 'http://127.0.0.1:%d' % free_port()
        cls.data = cls.pkg.root / 'waypost-data'
        env = {key: value for key, value in os.environ.items() if not key.startswith('WAYPOST_')}
        env['WAYPOST_PUBLIC_ORIGIN'] = cls.origin
        cls.log = (cls.pkg.root / 'waypost.log').open('w')
        cls.service = subprocess.Popen([str(Path(binary).resolve()), 'serve', '--data', str(cls.data),
            '--listen', cls.origin.split('//')[1]], env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                urllib.request.urlopen(cls.origin + '/health', timeout=1).close()
                break
            except OSError:
                time.sleep(.03)
        else:
            cls.tearDownClass()
            raise RuntimeError('Disposable Waypost service did not start.')
        cls.control = Api(cls.origin, (cls.data / 'admin-secret').read_text().strip())
        cls.user = cls.pkg.new_home('bridge isolated user')
        shim = cls.pkg.root / 'loopback python'
        shim.write_text(loopback.SHIM)
        shim.chmod(0o755)
        cls.extra = {'YOBIRU_PYTHON': str(shim), 'TM_TEST_REAL_PYTHON': sys.executable,
                     'TM_TEST_HARNESS': str(SUPPORT / 'loopback_harness.py')}

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'service', None):
            cls.service.terminate()
            cls.service.wait(timeout=10)
            cls.log.close()
        if getattr(cls, 'pkg', None):
            cls.pkg.cleanup()

    def setUp(self):
        self.prefix = uuid.uuid4().hex
        self.folder = self.pkg.root / self.prefix
        self.folder.mkdir(mode=0o700)
        self.processes = []
        self.addCleanup(self.stop_receivers)
        self.port = {name: free_port() for name in ('alpha', 'bravo')}
        self.home = {}
        for name, peer, agent in [('alpha', 'bravo', 'grok'), ('bravo', 'alpha', 'codex')]:
            home = self.folder / name
            home.mkdir(mode=0o700)
            self.home[name] = home
            state = self.pkg.fake_state(self.prefix + name,
                status=loopback.node(name, peer), whois=loopback.whois(peer))
            config = {'port': self.port[name], 'bind': '127.0.0.1', 'self_agent': agent,
                      'default_agent': agent, 'agents': {agent: {}},
                      'tailscale_cmd': [sys.executable, str(SUPPORT / 'fake_tailscale.py'),
                                        '--state', str(state)]}
            (home / 'config.json').write_text(json.dumps(config))
            log = (self.folder / (name + '.log')).open('w')
            process = subprocess.Popen(['/bin/sh', str(self.pkg.launcher), '--home', str(home), 'serve'],
                env=self.pkg.env(self.user, extra=self.extra), cwd=str(self.pkg.work),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
            self.processes.append(process)
        for name in self.home:
            self.assertTrue(loopback.wait_for(lambda: loopback.health(self.port[name])))
        self.space, self.root, self.bridge_token, self.primary = self.space_fixture('trial')
        self.token_file = self.folder / 'bridge-token'
        self.token_file.write_text(self.bridge_token)
        self.token_file.chmod(0o600)
        self.route_file = self.folder / 'routes.json'
        self.route_file.write_text(json.dumps({
            'incoming': {'alpha.example.ts.net/grok': self.primary_actor},
            'outgoing': {self.primary_actor: 'alpha:%d/grok' % self.port['alpha']}}))
        self.journal = self.folder / 'journal.sqlite3'

    def stop_receivers(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)

    def space_fixture(self, suffix):
        space = self.control.call('POST', '/admin/v0/spaces', {
            'request_id': self.prefix + suffix, 'display_name': 'Bridge synthetic test'})
        root = '/admin/v0/spaces/' + space['space_id']
        identities = {}
        for name in ('bridge', 'primary'):
            actor = self.control.call('POST', root + '/actors', {
                'request_id': self.prefix + suffix + name, 'display_name': name, 'kind': 'agent'})
            consumer = self.control.call('POST', root + '/consumers', {
                'request_id': self.prefix + suffix + name + '-consumer',
                'actor_id': actor['actor_id'], 'view': 'inbox', 'filter': {}})
            identities[name] = (actor, consumer)
        tokens = {}
        for name, (actor, consumer) in identities.items():
            recipient = identities['primary' if name == 'bridge' else 'bridge'][0]['actor_id']
            tokens[name] = self.control.call('POST', root + '/tokens', {
                'request_id': self.prefix + suffix + name + '-token',
                'actor_id': actor['actor_id'], 'consumer_id': consumer['consumer_id'],
                'scopes': ['message.send', 'message.read', 'receipt.write'],
                'resource_grants': {'recipient_ids': [recipient]}})['token']
        self.primary_actor = identities['primary'][0]['actor_id']
        self.bridge_actor = identities['bridge'][0]['actor_id']
        return space, root, tokens['bridge'], Api(self.origin, tokens['primary'])

    def ysend(self, text, *extra):
        result = self.pkg.run(['--home', str(self.home['alpha']), 'send',
            'bravo:%d/codex' % self.port['bravo'], text, '--wake', 'none', *extra],
            self.user, extra=self.extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.splitlines()[-1])['id']

    def run_bridge(self, *flags, launcher=None, origin=None):
        return subprocess.run([sys.executable, str(ROOT / 'tools/waypost_bridge.py'),
            '--yobiru', str(launcher or self.pkg.launcher), '--home', str(self.home['bravo']),
            '--waypost-url', origin or self.origin, '--token-file', str(self.token_file),
            '--map', str(self.route_file), '--state', str(self.journal), '--once', *flags],
            env=self.pkg.env(self.user, extra=self.extra), capture_output=True, text=True, timeout=45)

    def inspect(self, kind):
        items, cursor = [], None
        while True:
            page = self.control.call('POST', self.root + '/inspect', {'kind': kind, 'limit': 20, 'cursor': cursor})
            items.extend(page['items'])
            if not page['has_more']:
                return items
            cursor = page['next_cursor']

    def records(self, who):
        return [json.loads(path.read_text()) for path in (self.home[who] / 'inbox').glob('*.json')]

    def send_to_bridge(self, request_id='reverse-report'):
        return self.primary.call('POST', '/v0/messages', {
            'request_id': self.prefix + request_id, 'recipient_ids': [self.bridge_actor],
            'kind': 'result', 'subject': 'Synthetic result', 'body': {'text': 'Budget 680; no purchase.'}})

    def test_incoming_dry_run_then_restart_imports_once_with_bridge_attribution(self):
        mid = self.ysend('Synthetic booking: Friday 19:00. Not a real reservation.')
        dry = self.run_bridge('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.inspect('messages'), [])
        self.assertNotIn('Friday 19:00', dry.stdout)
        for _ in range(2):
            result = self.run_bridge()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        messages = self.inspect('messages')
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['producer_id'], self.bridge_actor)
        message = self.control.call('POST', self.root + '/inspect',
            {'kind': 'message', 'resource_id': messages[0]['message_id']})
        encoded = json.dumps(message)
        self.assertIn(mid, encoded)
        self.assertIn('alpha.example.ts.net', encoded)
        self.assertIn('Friday 19:00', encoded)

    def test_reverse_real_yobiru_delivery_and_receipts_survive_restart(self):
        sent = self.send_to_bridge()
        for _ in range(2):
            result = self.run_bridge()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.records('alpha')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['received']['peer']['node'], 'bravo.example.ts.net')
        self.assertEqual(records[0]['envelope']['wake'], 'none')
        self.assertIn(sent['message_id'], records[0]['envelope']['text'])
        self.assertIn('Budget 680', records[0]['envelope']['text'])
        receipts = self.inspect('receipts')
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['stage'], 2)  # Administrative rows encode stages as integers.
        self.assertEqual(receipts[0]['version'], 2)

    def test_lost_yobiru_response_stays_unknown_and_never_resends_or_claims_processed(self):
        sent = self.send_to_bridge()
        launcher = self.folder / 'lost-response.sh'
        launcher.write_text('#!/bin/sh\n/bin/sh ' + repr(str(self.pkg.launcher)) + ' "$@" >/dev/null\nexit 99\n')
        first = self.run_bridge(launcher=launcher)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(len(self.records('alpha')), 1)
        second = self.run_bridge()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(len(self.records('alpha')), 1)
        self.assertEqual([row['stage'] for row in self.inspect('receipts')], [1])
        # The operator verifies the actual receiver record before confirming it.
        item = next(row for row in self.inspect('feed') if row['resource_id'] == sent['message_id'])
        confirmed_id = self.records('alpha')[0]['envelope']['id']
        reconciled = self.run_bridge('--confirm-sent', item['item_id'], confirmed_id)
        self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
        self.assertEqual(len(self.records('alpha')), 1)
        self.assertEqual([row['stage'] for row in self.inspect('receipts')], [2])

    def test_zero_exit_with_a_mismatched_peer_receipt_remains_unknown(self):
        self.send_to_bridge()
        launcher = self.folder / 'unconfirmed-result.sh'
        launcher.write_text("#!/bin/sh\nprintf '%s\\n' '{\"status\":202,\"id\":\"abcdefgh\",\"response\":{\"ok\":true,\"id\":\"different\"}}'\n")
        first = self.run_bridge(launcher=launcher)
        self.assertNotEqual(first.returncode, 0)
        self.assertNotEqual(self.run_bridge().returncode, 0)
        self.assertEqual(self.records('alpha'), [])
        self.assertEqual([row['stage'] for row in self.inspect('receipts')], [1])

    def test_journal_reuse_with_another_space_or_route_is_refused(self):
        first = self.run_bridge()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        routes = json.loads(self.route_file.read_text())
        routes['outgoing'][self.primary_actor] = 'alpha:%d/other' % self.port['alpha']
        self.route_file.write_text(json.dumps(routes))
        changed = self.run_bridge()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('JOURNAL_IDENTITY_OR_ROUTES_CHANGED', changed.stderr)
        self.space, self.root, self.bridge_token, self.primary = self.space_fixture('other-space')
        self.token_file.write_text(self.bridge_token)
        self.route_file.write_text(json.dumps({
            'incoming': {'alpha.example.ts.net/grok': self.primary_actor},
            'outgoing': {self.primary_actor: 'alpha:%d/grok' % self.port['alpha']}}))
        changed = self.run_bridge()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('JOURNAL_IDENTITY_OR_ROUTES_CHANGED', changed.stderr)
        self.assertEqual(self.inspect('messages'), [])

    def test_more_than_one_page_is_forwarded_without_dropping_items(self):
        for index in range(41):
            self.send_to_bridge('page-%d' % index)
        # A page budget is a batch boundary: the run forwards and acknowledges what it read, and the next run
        # resumes from the saved cursor, here reading two pages at once.
        budget = self.run_bridge('--max-pages', '1')
        self.assertEqual(budget.returncode, 0, budget.stdout + budget.stderr)
        self.assertEqual((20, 20), (len(self.records('alpha')), len(self.inspect('receipts'))))
        result = self.run_bridge()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records('alpha')), 41)
        receipts = self.inspect('receipts')
        self.assertEqual(len(receipts), 41)
        self.assertTrue(all(row['stage'] == 2 for row in receipts))

    def test_lost_waypost_response_retries_the_identical_write_without_duplicates(self):
        self.ysend('Persist this synthetic result exactly once.')
        target_port = int(self.origin.rsplit(':', 1)[1])
        dropped = []
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def forward(self):
                body = self.rfile.read(int(self.headers.get('content-length', '0')))
                connection = http.client.HTTPConnection('127.0.0.1', target_port, timeout=10)
                connection.request(self.command, self.path, body,
                    {key: value for key, value in self.headers.items() if key.lower() not in ('host', 'connection')})
                response = connection.getresponse()
                payload, status = response.read(), response.status
                connection.close()
                if self.path == '/v0/messages' and not dropped:
                    dropped.append(True)
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            do_GET = do_POST = forward
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            origin = 'http://127.0.0.1:%d' % proxy.server_port
            self.assertNotEqual(self.run_bridge(origin=origin).returncode, 0)
            self.assertEqual(len(self.inspect('messages')), 1)
            result = self.run_bridge(origin=origin)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(self.inspect('messages')), 1)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)

    def refusals(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [(row['refused'], row.get('item_id')) for row in map(json.loads, result.stderr.splitlines())
                if 'refused' in row]

    def test_missing_authenticated_peer_and_attachments_do_not_get_partially_forwarded(self):
        mid = self.ysend('Local receipt is authoritative.')
        path = self.home['bravo'] / 'inbox' / (mid + '.json')
        record = json.loads(path.read_text())
        del record['received']['peer']
        path.write_text(json.dumps(record))
        self.assertEqual(self.refusals(self.run_bridge()), [('MISSING_AUTHENTICATED_PEER', mid)])
        self.assertEqual(self.inspect('messages'), [])
        record['received']['peer'] = {'node': 'alpha.example.ts.net'}
        record['envelope']['from'] = 'other.example.ts.net'
        path.write_text(json.dumps(record))
        self.assertEqual(self.refusals(self.run_bridge()), [], 'a changed record stays quarantined until retried')
        self.assertEqual(self.refusals(self.run_bridge('--retry-refused', mid)), [])
        self.assertEqual(self.refusals(self.run_bridge()), [('AUTHENTICATED_SENDER_MISMATCH', mid)])
        self.assertEqual(self.inspect('messages'), [])
        path.unlink()
        attachment = self.folder / 'synthetic.txt'
        attachment.write_text('Do not silently omit me.')
        with_file = self.ysend('Has an attachment.', '--file', str(attachment))
        self.assertEqual(self.refusals(self.run_bridge()), [('ATTACHMENTS_UNSUPPORTED', with_file)])
        self.assertEqual(self.inspect('messages'), [])

    def test_an_unrelated_inbox_message_is_quarantined_and_never_halts_the_bridge(self):
        mapped = self.ysend('Mapped synthetic report.')
        unrelated = self.ysend('Ordinary unrelated agent report.', '--as', 'unmapped')
        self.assertEqual(self.refusals(self.run_bridge()), [('UNKNOWN_INCOMING_ROUTE', unrelated)])
        messages = self.inspect('messages')
        self.assertEqual(len(messages), 1)
        self.assertNotIn('unrelated agent report', json.dumps(messages))
        (self.home['bravo'] / 'inbox' / (unrelated + '.json')).unlink()
        self.assertEqual(self.refusals(self.run_bridge()), [], 'reported once; removal changes nothing')
        listed = self.run_bridge('--list-refused')
        self.assertEqual([{'direction': 'incoming', 'item_id': unrelated, 'state': 'refused',
                           'error': 'UNKNOWN_INCOMING_ROUTE'}], [json.loads(x) for x in listed.stdout.splitlines()])
        self.assertEqual(len(self.inspect('messages')), 1)
        message = self.control.call('POST', self.root + '/inspect',
                                    {'kind': 'message', 'resource_id': messages[0]['message_id']})
        self.assertIn(mapped, json.dumps(message))


if __name__ == '__main__':
    unittest.main()
