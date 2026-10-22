#!/usr/bin/env python3
"""Opt-in text-only Yobiru <-> Waypost bridge; POSIX, Python 3.8+, stdlib only.

Routes are operator configuration, never instructions from message bodies.
The private SQLite journal is required for recovery. Preserve it: ordinary
Yobiru send cannot supply a stable envelope ID, so uncertain sends must be
reconciled by a human and are never automatically sent again. A processed
receipt means the bridge confirmed forwarding, not that a remote task ran.
A message the bridge refuses (no route, unverified sender, attachments, an
unreadable record) is quarantined in the journal: recorded once, never
forwarded or acknowledged, and it never stops the bridge. The decision
belongs to the inbox file and holds when the file later changes, until an
explicit --retry-refused.
"""
import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENVELOPE_ID = re.compile(r'[A-Za-z0-9_-]{8,64}\Z')
RESOURCE_ID = re.compile(r'[A-Za-z0-9_-]{1,96}\Z')
HOST = re.compile(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z')
MAX_BYTES = 2 * 1024 * 1024
RECEIPT_MAP = {'received': 'received', 'running': 'received',
               'completed': 'processed', 'failed': 'processed',
               'rejected': 'processed', 'blocked': 'processed'}


class BridgeError(Exception):
    def __init__(self, code, retryable=False, item=None):
        super().__init__(code)
        self.code, self.retryable, self.item = code, retryable, item


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('non-finite JSON number')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def valid_envelope_id(value):
    return isinstance(value, str) and ENVELOPE_ID.fullmatch(value) is not None


def valid_resource(value):
    return isinstance(value, str) and RESOURCE_ID.fullmatch(value) is not None


def stable_request_id(message_id, space=None, peer=None, agent=None):
    """Compatibility helper; live keys additionally bind space + verified peer + route."""
    if not valid_envelope_id(message_id):
        raise ValueError('cannot correlate an invalid message id')
    material = message_id if space is None else canonical([space, peer, agent, message_id])
    return 'yb-' + hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]


def normalize_agent(name):
    import unicodedata
    if not isinstance(name, str):
        raise ValueError('agent name must be text')
    cleaned = unicodedata.normalize('NFC', name)
    if not 1 <= len(cleaned) <= 64 or cleaned != cleaned.strip() or '/' in cleaned:
        raise ValueError('invalid agent name')
    if any(unicodedata.category(c) in ('Cc', 'Zl', 'Zp') for c in cleaned):
        raise ValueError('invalid agent name')
    return cleaned


def short_host(fqdn):
    return fqdn.split('.')[0]


def waypost_actor_for_sender(from_host, from_agent):
    """Display-only legacy convention. Live routing never uses this as an identity."""
    return '%s/%s' % (short_host(from_host), normalize_agent(from_agent))


def inject_correlation(payload, request_id, in_reply_to=None):
    if not valid_envelope_id(request_id):
        raise ValueError('request_id must be a valid envelope id')
    body = dict(payload)
    body['waypost_request_id'] = request_id
    if in_reply_to is not None:
        body['waypost_in_reply_to'] = in_reply_to
    return body


def extract_correlation(payload):
    if not isinstance(payload, dict) or not valid_envelope_id(payload.get('waypost_request_id')):
        return None, None
    return payload['waypost_request_id'], payload.get('waypost_in_reply_to')


def map_receipt_state(state):
    """Pure lifecycle vocabulary helper, not the live bridge's forwarding policy."""
    return RECEIPT_MAP.get(state)


def private_file(path, create=False):
    if os.name != 'posix':
        raise ValueError('private-file checks require POSIX')
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    if create:
        flags |= os.O_CREAT
    if Path(path).is_symlink():
        raise ValueError('private file must not be a symbolic link')
    fd = os.open(str(path), flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError('private file must be owned by this user and mode 0600 or stricter')
    return fd


def read_token(path):
    fd = private_file(path)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        token = handle.read(4097).strip()
    if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('invalid token file')
    return token


def valid_host(value, fqdn=False):
    return (isinstance(value, str) and len(value) <= 253 and HOST.fullmatch(value)
            and '..' not in value and (not fqdn or '.' in value))


def load_map(path):
    if not path:
        raise ValueError('an explicit route map is required')
    data = strict_json(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'incoming', 'outgoing'}:
        raise ValueError('map must contain incoming and outgoing objects')
    incoming, outgoing = data['incoming'], data['outgoing']
    if not isinstance(incoming, dict) or not isinstance(outgoing, dict):
        raise ValueError('routes must be objects')
    for key, actor in incoming.items():
        peer, separator, agent = key.partition('/')
        if (not separator or not valid_host(peer, True) or normalize_agent(agent) != agent
                or not valid_resource(actor)):
            raise ValueError('invalid incoming route')
    for producer, address in outgoing.items():
        if not valid_resource(producer) or not isinstance(address, str):
            raise ValueError('invalid outgoing route')
        hostport, separator, agent = address.partition('/')
        host, colon, port = hostport.partition(':')
        if (not separator or not valid_host(host) or normalize_agent(agent) != agent
                or (colon and (not port.isdigit() or not 1 <= int(port) <= 65535))):
            raise ValueError('invalid outgoing address')
    return data


def read_inbox_envelopes(home):
    inbox = Path(home) / 'inbox'
    if not inbox.exists():
        return
    if inbox.is_symlink() or not inbox.is_dir():
        raise BridgeError('UNSAFE_INBOX')
    for child in sorted(inbox.glob('*.json')):
        # One bad record must not stop the bridge: it is yielded with its problem and quarantined.
        if child.is_symlink() or not child.is_file() or child.stat().st_size > MAX_BYTES:
            yield child.stem, None, None, 'UNSAFE_INBOX_RECORD'
            continue
        try:
            record = strict_json(child.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            yield child.stem, None, None, 'INVALID_INBOX_RECORD'
            continue
        if not isinstance(record, dict) or not isinstance(record.get('envelope'), dict) or not isinstance(record.get('received'), dict):
            yield child.stem, None, None, 'INVALID_INBOX_RECORD'
            continue
        yield child.stem, record['envelope'], record['received'], None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BridgeError('WAYPOST_REDIRECT_REFUSED')


def waypost_request(base_url, token, method, route, body=None):
    data = None if method == 'GET' else canonical(body if body is not None else {}).encode('utf-8')
    request = urllib.request.Request(base_url.rstrip('/') + route, data=data, method=method)
    request.add_header('Content-Type', 'application/json')
    request.add_header('User-Agent', 'Waypost/0.1.0')
    request.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise BridgeError('WAYPOST_RESPONSE_TOO_LARGE')
        value = strict_json(raw.decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise BridgeError('WAYPOST_HTTP_%d' % error.code, error.code == 429 or error.code >= 500)
    except (OSError, urllib.error.URLError):
        raise BridgeError('WAYPOST_CONNECTION_FAILED', True)
    except (ValueError, UnicodeError):
        raise BridgeError('WAYPOST_INVALID_RESPONSE')
    if not isinstance(value, dict) or value.get('ok') is not True or not isinstance(value.get('data'), dict):
        raise BridgeError('WAYPOST_INVALID_RESPONSE')
    return value['data']


def yobiru_cli(launcher, home, *args):
    return subprocess.run(['sh', str(launcher), '--home', str(home)] + list(args),
                          capture_output=True, text=True, timeout=120)


class Journal:
    def __init__(self, path, binding):
        import fcntl
        self.path = Path(path).absolute()
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('journal directory must be private and not a symbolic link')
        for suffix in ('', '-journal', '-wal', '-shm', '.lock'):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists() or candidate.is_symlink():
                fd = private_file(candidate)
                os.close(fd)
        self.lock_fd = private_file(str(self.path) + '.lock', True)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise BridgeError('JOURNAL_ALREADY_LOCKED')
        self.db = None
        try:
            fd = private_file(self.path, True)
            os.close(fd)
            self.db = sqlite3.connect(str(self.path), timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS incoming(
                  source_key TEXT PRIMARY KEY,envelope_id TEXT NOT NULL,record TEXT NOT NULL,
                  payload TEXT,error TEXT,state TEXT NOT NULL,waypost_message_id TEXT);
                CREATE INDEX IF NOT EXISTS incoming_by_file ON incoming(envelope_id);
                CREATE TABLE IF NOT EXISTS outgoing(
                  item_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,producer_id TEXT NOT NULL,
                  feed TEXT NOT NULL,message TEXT NOT NULL,address TEXT,state TEXT NOT NULL,
                  received_acked INTEGER NOT NULL DEFAULT 0,processed_acked INTEGER NOT NULL DEFAULT 0,
                  yobiru_id TEXT,confirmation TEXT,error TEXT);
            ''')
            stored = self.get('binding')
            if stored is not None and stored != canonical(binding):
                raise BridgeError('JOURNAL_IDENTITY_OR_ROUTES_CHANGED')
            with self.db:
                if stored is None:
                    self.db.execute('INSERT INTO meta VALUES(?,?)', ('binding', canonical(binding)))
                # A process could have died after the peer stored a message. Do not repeat send.
                self.db.execute("UPDATE outgoing SET state='unknown',error='INTERRUPTED_SEND' WHERE state='attempting'")
                # Earlier builds kept refusals as pending rows that failed every run: quarantine them.
                self.db.execute("UPDATE incoming SET state='refused' WHERE state='pending' AND error IS NOT NULL")
                self.db.execute("UPDATE outgoing SET state='refused' WHERE state='pending' AND error IS NOT NULL")
        except BaseException:
            self.close()
            raise

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return None if row is None else row[0]

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class Bridge:
    def __init__(self, args, token):
        self.args, self.token = args, token
        self.routes = load_map(args.map)
        self.description = waypost_request(args.waypost_url, token, 'GET', '/v0/describe')
        self.identity = {key: self.description.get(key) for key in ('space_id', 'actor_id', 'consumer_id')}
        if not all(valid_resource(value) for value in self.identity.values()):
            raise BridgeError('INVALID_BRIDGE_IDENTITY')
        if not {'message.send', 'message.read', 'receipt.write'}.issubset(self.description.get('scopes', [])):
            raise BridgeError('MISSING_BRIDGE_SCOPES')
        limit = self.description.get('limits', {}).get('max_page_size')
        if type(limit) is not int or limit < 1:
            raise BridgeError('INVALID_WAYPOST_LIMITS')
        self.limit = min(20, limit)
        self.max_json = self.description.get('limits', {}).get('max_json_bytes', 16384)
        if type(self.max_json) is not int or self.max_json < 1:
            raise BridgeError('INVALID_WAYPOST_LIMITS')
        allowed = self.description.get('allowed_recipient_ids')
        if not isinstance(allowed, list) or not set(self.routes['incoming'].values()).issubset(allowed):
            raise BridgeError('ROUTES_EXCEED_RECIPIENT_GRANT')
        if self.identity['actor_id'] in self.routes['outgoing']:
            raise BridgeError('BRIDGE_ACTOR_CANNOT_BE_AN_OUTGOING_ROUTE')
        self.journal = None
        if not args.dry_run:
            binding = dict(self.identity, routes=self.routes, home=str(Path(args.home).resolve()),
                           waypost_url=args.waypost_url.rstrip('/'), version=1)
            self.journal = Journal(args.state, binding)

    def close(self):
        if self.journal:
            self.journal.close()

    def incoming_plan(self, mid, envelope, received):
        peer = received.get('peer')
        node = peer.get('node') if isinstance(peer, dict) else None
        if not valid_host(node, True):
            raise BridgeError('MISSING_AUTHENTICATED_PEER', item=mid)
        claimed = envelope.get('from')
        if not isinstance(claimed, str) or claimed.lower().rstrip('.') != node.lower().rstrip('.'):
            raise BridgeError('AUTHENTICATED_SENDER_MISMATCH', item=mid)
        if not valid_envelope_id(mid) or envelope.get('id') != mid:
            raise BridgeError('INVALID_ENVELOPE_ID')
        try:
            agent = normalize_agent(envelope.get('from_agent'))
        except ValueError:
            raise BridgeError('MISSING_OR_INVALID_SOURCE_AGENT', item=mid)
        recipient = self.routes['incoming'].get(node + '/' + agent)
        if recipient is None:
            raise BridgeError('UNKNOWN_INCOMING_ROUTE', item=mid)
        if envelope.get('fetch') not in (None, []):
            raise BridgeError('ATTACHMENTS_UNSUPPORTED', item=mid)
        if not isinstance(envelope.get('text', ''), str):
            raise BridgeError('INVALID_MESSAGE_TEXT', item=mid)
        payload = {'request_id': stable_request_id(mid, self.identity['space_id'], node, agent),
                   'recipient_ids': [recipient], 'kind': 'info', 'subject': 'Yobiru message ' + mid,
                   'body': {'text': canonical({'type': 'yobiru-message', 'authenticated_peer': node,
                                              'source_agent': agent, 'envelope': envelope})}}
        if len(canonical(payload).encode('utf-8')) > self.max_json:
            raise BridgeError('MESSAGE_EXCEEDS_WAYPOST_LIMIT', item=mid)
        return payload

    def poll_yobiru_once(self):
        failures, refused, count = [], [], 0
        if not self.args.dry_run:
            db = self.journal.db
            # Earlier builds could plan one inbox file again after its record changed. While none of those plans
            # is known to be sent, which one may go is ambiguous: quarantine them all, reported once.
            for row in db.execute("SELECT envelope_id FROM incoming GROUP BY envelope_id "
                                  "HAVING COUNT(*)>1 AND SUM(state='pending')=COUNT(*)").fetchall():
                with db:
                    db.execute("UPDATE incoming SET state='refused',error='PLANNED_MORE_THAN_ONCE' "
                               "WHERE envelope_id=?", (row[0],))
                refused.append(BridgeError('PLANNED_MORE_THAN_ONCE', item=row[0]))
        for mid, envelope, received, problem in read_inbox_envelopes(self.args.home):
            if problem:
                # Nothing in an unreadable record can be trusted, so its file name is the only key.
                record = canonical({'unreadable_record': mid})
                key = hashlib.sha256(canonical([self.identity['space_id'], 'unreadable', mid]).encode('utf-8')).hexdigest()
            else:
                record = canonical({'envelope': envelope, 'received': received})
                # Local inbox filenames are unique; the full immutable envelope plus verified
                # sender are recorded before any remote write. Mutable wake/ack fields are not keys.
                key = hashlib.sha256(canonical([self.identity['space_id'], mid,
                    received.get('peer'), envelope.get('from_agent')]).encode('utf-8')).hexdigest()
            payload, error = None, problem
            if not problem:
                try:
                    payload = self.incoming_plan(mid, envelope, received)
                except BridgeError as exc:
                    error = exc.code
            if self.args.dry_run:
                if error:
                    refused.append(BridgeError(error, item=mid))
                else:
                    count += 1
                continue
            db = self.journal.db
            # Every decision belongs to the inbox file (envelope_id holds its name), not to what the file
            # holds now: the key changes when a record becomes readable or names another sender.
            rows = db.execute('SELECT source_key,record,state FROM incoming WHERE envelope_id=?', (mid,)).fetchall()
            if any(row['state'] in ('refused', 'dismissed') for row in rows):
                continue  # Quarantined whatever the file holds now, until an explicit --retry-refused.
            if rows:
                # The forwarded record, once there is one, is what the file must still hold.
                sent = [row for row in rows if row['state'] == 'sent']
                same = [row for row in sent or rows if row['source_key'] == key]
                if not problem and (not same or strict_json(same[0]['record']).get('envelope') != envelope):
                    raise BridgeError('INBOX_RECORD_CONFLICT', item=mid)
                continue  # Planned or forwarded once; a file that is now unreadable leaves nothing to compare.
            # A refusal is quarantined at once: recorded with its reason, reported this time only, never
            # forwarded, and never a reason to stop the bridge or to retry.
            with db:
                db.execute('INSERT INTO incoming VALUES(?,?,?,?,?,?,NULL)',
                           (key, mid, record, canonical(payload) if payload else None, error,
                            'refused' if error else 'pending'))
            if error:
                refused.append(BridgeError(error, item=mid))
        if self.args.dry_run:
            return count, refused, failures
        db = self.journal.db
        # One forward per inbox file: only a file's sole row is ever dispatched. A plan an earlier build left next
        # to a refusal, a sent record or another plan is never sent, and its outcome is never claimed.
        for row in db.execute("SELECT * FROM incoming AS plan WHERE state='pending' AND (SELECT COUNT(*) FROM "
                              "incoming WHERE envelope_id=plan.envelope_id)=1 ORDER BY source_key").fetchall():
            try:
                result = waypost_request(self.args.waypost_url, self.token, 'POST', '/v0/messages', strict_json(row['payload']))
                if result.get('submission_state') != 'committed' or not valid_resource(result.get('message_id')):
                    raise BridgeError('WAYPOST_SEND_NOT_CONFIRMED', True)
                with db:
                    db.execute("UPDATE incoming SET state='sent',waypost_message_id=? WHERE source_key=?",
                               (result['message_id'], row['source_key']))
                count += 1
            except BridgeError as exc:
                exc.item = row['envelope_id']
                failures.append(exc)
        return count, refused, failures

    def ingest_waypost(self):
        cursor = None if self.args.dry_run else self.journal.get('cursor')
        planned, refused, failures = 0, [], []
        for _ in range(self.args.max_pages):
            page = waypost_request(self.args.waypost_url, self.token, 'POST', '/v0/inbox/read',
                {'consumer_id': self.identity['consumer_id'], 'cursor': cursor, 'limit': self.limit})
            if not isinstance(page.get('items'), list) or type(page.get('has_more')) is not bool:
                raise BridgeError('INVALID_WAYPOST_PAGE')
            for item in page['items']:
                if not isinstance(item, dict):
                    raise BridgeError('INVALID_WAYPOST_ITEM')
                if item.get('item_type') != 'message':
                    continue  # Outside text-forwarding scope; never acknowledge these facts.
                if (not all(valid_resource(item.get(k)) for k in ('item_id', 'resource_id', 'producer_id'))
                        or item.get('space_id') != self.identity['space_id']):
                    raise BridgeError('INVALID_WAYPOST_ITEM')
                mid = item['item_id']
                if not self.args.dry_run:
                    existing = self.journal.db.execute('SELECT feed FROM outgoing WHERE item_id=?', (mid,)).fetchone()
                    if existing is not None:
                        if existing['feed'] != canonical(item):
                            raise BridgeError('WAYPOST_FEED_CONFLICT', item=mid)
                        continue
                message = waypost_request(self.args.waypost_url, self.token, 'GET', '/v0/messages/' + item['resource_id'])
                if (message.get('message_id') != item['resource_id'] or message.get('producer_id') != item['producer_id']
                        or message.get('space_id') != self.identity['space_id']):
                    raise BridgeError('WAYPOST_MESSAGE_MISMATCH', item=mid)
                error = None
                address = self.routes['outgoing'].get(item['producer_id'])
                if address is None:
                    error = 'UNKNOWN_OUTGOING_ROUTE'
                if message.get('attachment_ids') not in (None, []):
                    error = 'ATTACHMENTS_UNSUPPORTED'
                if not isinstance(message.get('body'), dict) or not isinstance(message['body'].get('text'), str):
                    error = 'INVALID_WAYPOST_MESSAGE'
                if self.args.dry_run:
                    if error:
                        refused.append(BridgeError(error, item=mid))
                    else:
                        planned += 1
                    continue
                # A refused item is quarantined: never sent to Yobiru and never acknowledged in Waypost.
                with self.journal.db:
                    self.journal.db.execute('''INSERT INTO outgoing
                        (item_id,resource_id,producer_id,feed,message,address,state,error)
                        VALUES(?,?,?,?,?,?,?,?)''', (mid,item['resource_id'],item['producer_id'],
                        canonical(item),canonical(message),address,'refused' if error else 'pending',error))
                if error:
                    refused.append(BridgeError(error, item=mid))
            next_cursor = page.get('next_cursor')
            if not isinstance(next_cursor, str) or (page['has_more'] and next_cursor == cursor):
                raise BridgeError('INVALID_WAYPOST_CURSOR')
            cursor = next_cursor
            if not self.args.dry_run:
                with self.journal.db:
                    self.journal.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('cursor', cursor))
            if not page['has_more']:
                return planned, refused, failures
        # The page budget is a batch boundary, not a failure: this run forwards and acknowledges what it read,
        # and the next one resumes from the saved cursor.
        return planned, refused, failures

    def ack(self, item_id, stage):
        request_id = 'yb-ack-' + hashlib.sha256(canonical([self.identity['space_id'],
            self.identity['consumer_id'],item_id,stage]).encode('utf-8')).hexdigest()[:40]
        result = waypost_request(self.args.waypost_url, self.token, 'POST', '/v0/receipts',
            {'request_id':request_id,'consumer_id':self.identity['consumer_id'],
             'items':[{'item_id':item_id,'stage':stage}]})
        items = result.get('items')
        if (result.get('submission_state') != 'committed' or result.get('consumer_id') != self.identity['consumer_id']
                or not isinstance(items,list) or len(items)!=1 or items[0].get('item_id')!=item_id
                or items[0].get('stage') not in ({'received','processed'} if stage=='received' else {'processed'})):
            raise BridgeError('WAYPOST_ACK_NOT_CONFIRMED', True, item_id)

    def process_outgoing(self, selected=None):
        db, sent, failures = self.journal.db, 0, []
        rows = db.execute("SELECT * FROM outgoing WHERE state NOT IN ('done','refused','dismissed') "
                          "ORDER BY item_id").fetchall()
        for row in rows:
            mid = row['item_id']
            if selected is not None and selected != mid:
                continue
            if row['state'] in ('unknown', 'attempting'):
                failures.append(BridgeError('YOBIRU_DELIVERY_UNKNOWN', item=mid))
                continue
            try:
                if not row['received_acked']:
                    self.ack(mid,'received')
                    with db:
                        db.execute('UPDATE outgoing SET received_acked=1 WHERE item_id=?',(mid,))
                if row['state']=='pending':
                    wrapper = canonical({'type':'waypost-message','waypost_space_id':self.identity['space_id'],
                        'waypost_message_id':row['resource_id'],'waypost_item_id':mid,
                        'message':strict_json(row['message'])})
                    # Commit before starting the non-idempotent CLI. A crash anywhere after
                    # this point is an unknown delivery, never an invitation to resend.
                    with db:
                        db.execute("UPDATE outgoing SET state='attempting' WHERE item_id=?",(mid,))
                    try:
                        run = yobiru_cli(self.args.yobiru,self.args.home,'send',row['address'],wrapper,
                                         '--kind','notice','--wake','none')
                        confirmation = strict_json(run.stdout.strip())
                        response = confirmation.get('response') if isinstance(confirmation,dict) else None
                        if (run.returncode!=0 or not isinstance(confirmation,dict)
                                or confirmation.get('status') not in (200,202)
                                or not valid_envelope_id(confirmation.get('id'))
                                or not isinstance(response,dict) or response.get('ok') is not True
                                or response.get('id')!=confirmation['id']):
                            raise BridgeError('YOBIRU_DELIVERY_UNKNOWN',item=mid)
                    except (OSError,subprocess.SubprocessError,ValueError,BridgeError):
                        with db:
                            db.execute("UPDATE outgoing SET state='unknown',error='UNCONFIRMED_CLI_RESULT' WHERE item_id=?",(mid,))
                        raise BridgeError('YOBIRU_DELIVERY_UNKNOWN',item=mid)
                    with db:
                        db.execute("UPDATE outgoing SET state='confirmed',yobiru_id=?,confirmation=?,error=NULL WHERE item_id=?",
                                   (confirmation['id'],canonical(confirmation),mid))
                    sent += 1
                self.ack(mid,'processed')
                with db:
                    db.execute("UPDATE outgoing SET state='done',processed_acked=1 WHERE item_id=?",(mid,))
            except BridgeError as exc:
                failures.append(exc)
        return sent, failures

    def reconcile(self, confirm=None, retry=None):
        db = self.journal.db
        mid = confirm[0] if confirm else retry
        row = db.execute('SELECT state FROM outgoing WHERE item_id=?',(mid,)).fetchone()
        if row is None or row['state']!='unknown':
            raise BridgeError('ONLY_UNKNOWN_DELIVERY_CAN_BE_RECONCILED')
        if confirm:
            if not valid_envelope_id(confirm[1]):
                raise BridgeError('INVALID_CONFIRMED_YOBIRU_ID')
            with db:
                db.execute("UPDATE outgoing SET state='confirmed',yobiru_id=?,confirmation=?,error=NULL WHERE item_id=?",
                           (confirm[1],canonical({'operator_confirmed':True,'id':confirm[1]}),mid))
            return self.process_outgoing(selected=mid)
        with db:
            db.execute("UPDATE outgoing SET state='pending',error=NULL WHERE item_id=?",(mid,))
        return 0, []  # A later explicit run performs the newly authorized retry.

    def refused(self):
        """Quarantined and dismissed items, oldest decisions first; reasons and ids only, never content."""
        rows = [('incoming', r['envelope_id'], r['state'], r['error']) for r in self.journal.db.execute(
            "SELECT envelope_id,state,error FROM incoming WHERE state IN ('refused','dismissed') ORDER BY rowid")]
        rows += [('outgoing', r['item_id'], r['state'], r['error']) for r in self.journal.db.execute(
            "SELECT item_id,state,error FROM outgoing WHERE state IN ('refused','dismissed') ORDER BY rowid")]
        return [{'direction': d, 'item_id': i, 'state': s, 'error': e} for d, i, s, e in rows]

    def resolve_refused(self, item, action):
        """dismiss: the operator has looked at a quarantined item; it stays unforwarded and unacknowledged.
        retry: forget an incoming refusal, dismissed or not, so that the next run plans the current inbox
        record again; this command forwards nothing. Either covers every row the journal holds for that inbox
        file. Outgoing refusals depend on the journal's fixed routes and cannot be retried."""
        db = self.journal.db
        held = ('refused',) if action == 'dismiss' else ('refused', 'dismissed')
        incoming = [r for r in db.execute('SELECT state FROM incoming WHERE envelope_id=?', (item,)) if r['state'] in held]
        outgoing = [r for r in db.execute('SELECT state FROM outgoing WHERE item_id=?', (item,)) if r['state'] in held]
        if bool(incoming) == bool(outgoing):
            raise BridgeError('ONLY_ONE_REFUSED_ITEM_CAN_BE_RESOLVED')
        with db:
            if action == 'dismiss' and incoming:
                db.execute("UPDATE incoming SET state='dismissed' WHERE envelope_id=? AND state='refused'", (item,))
            elif action == 'dismiss':
                db.execute("UPDATE outgoing SET state='dismissed' WHERE item_id=?", (item,))
            elif incoming:
                # Rows an earlier build planned next to the refusal go too; sent rows stay as the record.
                db.execute("DELETE FROM incoming WHERE envelope_id=? AND state IN ('refused','dismissed','pending')",
                           (item,))
            else:
                raise BridgeError('ONLY_INCOMING_REFUSALS_CAN_BE_RETRIED')

    def run_once(self):
        incoming, refused, failures = self.poll_yobiru_once()
        planned, remote_refused, remote_failures = self.ingest_waypost()
        failures += remote_failures
        refused += remote_refused
        if self.args.dry_run:
            return {'dry_run':True,'incoming_planned':incoming,'outgoing_planned':planned,
                    'refused_planned':len(refused),'sent':False}, refused, failures
        outgoing, remote_failures = self.process_outgoing()
        failures += remote_failures
        return {'incoming_sent':incoming,'outgoing_sent':outgoing,'refused':len(refused)}, refused, failures


def emit_errors(errors, key='error'):
    for error in errors:
        value = {key:error.code}
        if valid_resource(error.item):
            value['item_id'] = error.item
        print(json.dumps(value),file=sys.stderr)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yobiru',required=True,help='Yobiru launcher; invoked with sh')
    parser.add_argument('--home',required=True,help='explicit Yobiru data home')
    parser.add_argument('--waypost-url',required=True)
    parser.add_argument('--token-file',required=True,help='private token file, no symlinks')
    parser.add_argument('--map',required=True,help='explicit incoming/outgoing routes JSON')
    parser.add_argument('--state',help='private SQLite journal; preserve across restarts')
    parser.add_argument('--dry-run',action='store_true',help='read-only plan; no journal, send or ACK')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=float,default=5.0)
    parser.add_argument('--max-backoff',type=float,default=30.0)
    parser.add_argument('--max-failures',type=int,default=3)
    parser.add_argument('--max-pages',type=int,default=100)
    reconcile=parser.add_mutually_exclusive_group()
    reconcile.add_argument('--confirm-sent',nargs=2,metavar=('ITEM_ID','YOBIRU_ID'),help='ONLY after verifying this exact delivery on the remote peer; ACK without sending again')
    reconcile.add_argument('--retry-unsent',metavar='ITEM_ID',help='ONLY after verifying the unknown attempt did not reach the peer; authorize a later retry')
    reconcile.add_argument('--list-refused',action='store_true',help='print quarantined and dismissed items (ids and reasons only)')
    reconcile.add_argument('--dismiss-refused',metavar='ITEM_ID',help='mark a quarantined item as reviewed; it stays unforwarded and unacknowledged')
    reconcile.add_argument('--retry-refused',metavar='ENVELOPE_ID',help='forget an incoming refusal, dismissed or not, so the next run plans its current inbox record again')
    args=parser.parse_args(argv)
    bridge=None
    try:
        if os.name != 'posix':
            raise BridgeError('PLATFORM_UNSUPPORTED_POSIX_REQUIRED')
        url=urllib.parse.urlsplit(args.waypost_url)
        local = url.hostname == 'localhost'
        with contextlib.suppress(ValueError):
            local = local or ipaddress.ip_address(url.hostname or '').is_loopback
        if (url.scheme not in ('http','https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('','/') or (url.scheme=='http' and not local)):
            raise ValueError('Waypost must use HTTPS, or HTTP on loopback')
        if (not Path(args.home).is_dir() or not Path(args.yobiru).is_file()
                or not 0.1<=args.interval<=3600 or not args.interval<=args.max_backoff<=3600
                or not 1<=args.max_failures<=10 or not 1<=args.max_pages<=1000):
            raise ValueError('invalid bridge options')
        if args.dry_run and (args.confirm_sent or args.retry_unsent or args.list_refused or args.dismiss_refused
                             or args.retry_refused):
            raise ValueError('dry-run cannot reconcile delivery state')
        args.state=args.state or str(Path(args.home)/'waypost-bridge.sqlite3')
        old_mask=os.umask(0o077)
        try:
            bridge=Bridge(args,read_token(args.token_file))
            if args.list_refused:
                for row in bridge.refused():
                    print(json.dumps(row))
                return 0
            if args.dismiss_refused or args.retry_refused:
                bridge.resolve_refused(args.dismiss_refused or args.retry_refused,
                                       'dismiss' if args.dismiss_refused else 'retry')
                print(json.dumps({'resolved':args.dismiss_refused or args.retry_refused,
                                  'action':'dismissed' if args.dismiss_refused else 'retry_next_run'}))
                return 0
            if args.confirm_sent or args.retry_unsent:
                count,errors=bridge.reconcile(args.confirm_sent,args.retry_unsent)
                print(json.dumps({'reconciled':not bool(errors),'outgoing_sent':count}))
                emit_errors(errors)
                return 2 if errors else 0
            failures=0
            while True:
                try:
                    summary,refused,errors=bridge.run_once()
                    print(json.dumps(summary))
                    emit_errors(refused, 'refused')     # reported once; quarantine never stops the bridge
                except BridgeError as exc:
                    errors=[exc]
                emit_errors(errors)
                if args.once or args.dry_run:
                    return 2 if errors else 0
                if errors:
                    failures+=1
                    if failures>=args.max_failures or not all(error.retryable for error in errors):
                        return 2
                else:
                    failures=0
                time.sleep(min(args.max_backoff,args.interval*(2**failures)))
        finally:
            os.umask(old_mask)
    except BridgeError as exc:
        emit_errors([exc])
        return 2
    except (OSError,ValueError,sqlite3.Error,UnicodeError):
        emit_errors([BridgeError('INVALID_OR_UNAVAILABLE_BRIDGE_CONFIGURATION')])
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if bridge is not None:
            bridge.close()


if __name__=='__main__':
    raise SystemExit(main())
