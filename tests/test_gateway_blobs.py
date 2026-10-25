"""Blob store for large message bodies: with <home>/blobs.json a gateway writes bodies at or over blob_spill_bytes to
a local directory or an S3-compatible store (Cloudflare R2) and keeps only a `blob:<key>:<sha256>` reference in SQLite.
Callers and hosts see the same API; a store that is down or a body that was altered gives 503, never wrong content;
bodies no message references any more are collected.

Real bot_gateway.py subprocesses, a fake S3 server in this process that checks every AWS Signature Version 4, loopback.
"""
import base64
import datetime
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path

from support.public_harness import PUBLIC, FriendsGateway, plain_env
from test_friends_media import COUNTER, LIMITS, PDF, PNG, media

import bot_blobs as B  # noqa: E402
import bot_gateway as G  # noqa: E402

BIG_PNG = PNG + bytes(range(256)) * 40          # ~11 KiB, well over the test spill size
SPILL = dict(LIMITS, blob_spill_bytes=4096)
ACCESS, SECRET, BUCKET = "TESTKEY", "test-secret-access-key", "yobiru-test"


class FakeS3(object):
    """A minimal path-style S3: PUT/GET/DELETE of objects, each request's SigV4 signature recomputed and checked."""

    def __init__(self):
        self.objects, self.requests, self.down = {}, [], False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _check(self, body):
                fake.requests.append((self.command, self.path))
                if fake.down:
                    return 503
                when = datetime.datetime.strptime(self.headers.get("x-amz-date", ""), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=datetime.timezone.utc)
                signer = B.S3Store("http://%s" % self.headers["Host"], BUCKET, ACCESS, SECRET)
                want = signer.sign(self.command, self.path, body, now=when)["Authorization"]
                return 200 if self.headers.get("Authorization") == want else 403

            def _done(self, status, body=b""):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                status = self._check(body)
                if status == 200:
                    fake.objects[self.path] = body
                self._done(status)

            def do_GET(self):
                status = self._check(b"")
                if status != 200:
                    return self._done(status)
                if self.path not in fake.objects:
                    return self._done(404)
                self._done(200, fake.objects[self.path])

            def do_DELETE(self):
                status = self._check(b"")
                if status == 200:
                    fake.objects.pop(self.path, None)
                self._done(204 if status == 200 else status)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def private(path, text):
    path.write_text(text)
    os.chmod(str(path), 0o600)


class BlobUnitTests(unittest.TestCase):
    def test_signature_matches_the_aws_examples(self):
        # AWS S3 SigV4 documentation, "GET Bucket Lifecycle" and "Get Bucket (List Objects)"
        s = B.S3Store("https://examplebucket.s3.amazonaws.com", "examplebucket", "AKIAIOSFODNN7EXAMPLE",
                      "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region="us-east-1")
        when = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
        self.assertTrue(s.sign("GET", "/", b"", now=when, query="lifecycle=")["Authorization"].endswith(
            "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"))
        self.assertTrue(s.sign("GET", "/", b"", now=when, query="max-keys=2&prefix=J")["Authorization"].endswith(
            "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))

    def test_references_are_not_json_and_bodies_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            blobs = B.Blobs(B.LocalStore(Path(tmp)))
            text = json.dumps({"text": "menu", "attachments": [media()]})
            key, ref, stored = blobs.write(text)
            self.assertIsNone(B.parse_ref(text))
            self.assertEqual((key, B.parse_ref(ref)[1]), B.parse_ref(ref))
            self.assertRaises(ValueError, json.loads, ref)
            self.assertLess(stored, len(text))
            fresh = B.Blobs(B.LocalStore(Path(tmp)))        # no cache: read from the directory
            self.assertEqual(text, fresh.read(ref))
            self.assertEqual("{}", fresh.read("{}"))        # not a reference: the value itself
            path = Path(tmp) / key[:2] / key
            path.write_bytes(gzip.compress(b'{"text":"altered"}'))
            self.assertRaises(B.BlobError, B.Blobs(B.LocalStore(Path(tmp))).read, ref)
            path.write_bytes(b"not gzip")
            self.assertRaises(B.BlobError, B.Blobs(B.LocalStore(Path(tmp))).read, ref)

    def test_config_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(B.load(home))
            private(home / "blobs.json", json.dumps({"kind": "s3", "endpoint": "https://x.example", "bucket": "b",
                                                     "access_key_id": "k", "secret_access_key_file": "s"}))
            (home / "s").write_text("secret")
            os.chmod(str(home / "s"), 0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                B.load(home)
            os.chmod(str(home / "s"), 0o600)
            self.assertEqual("s3", B.load(home).kind)
            private(home / "blobs.json", json.dumps({"kind": "ftp"}))
            self.assertRaises(ValueError, B.load, home)
            private(home / "blobs.json", json.dumps({"kind": "local"}))
            self.assertEqual("local", B.load(home).kind)
            self.assertTrue((home / "blobs").is_dir())


class BlobGatewayCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru blobs test ")
        cls.root = Path(cls._tmp.name)
        cls.s3 = FakeS3()
        cls.gw = FriendsGateway(cls.root, limits=SPILL)
        private(cls.gw.home / "r2.secret", SECRET + "\n")
        private(cls.gw.home / "blobs.json", json.dumps({"kind": "s3", "endpoint": cls.s3.url, "bucket": BUCKET,
                                                        "access_key_id": ACCESS, "secret_access_key_file": "r2.secret",
                                                        "prefix": "gw/", "timeout_seconds": 5}))
        cls.gw.start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls.s3.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.s3.down = False

    def friends(self):
        n = next(COUNTER)
        a, a_source = self.gw.owned_bot("blob-a%d" % n)
        b, b_source = self.gw.owned_bot("blob-b%d" % n)
        self.assertEqual(201, self.gw.follow(b_source, a)[0])
        self.assertEqual(201, self.gw.follow(a_source, b)[0])
        wtoken, _ = self.gw.native(a)
        return a, wtoken, b_source, self.gw.conversation(a, b_source)

    def claimed(self, a, wtoken, cid, b_source, attachments, text="the menu"):
        extra = {"attachments": attachments} if attachments else {}
        status, sent = self.gw.send(cid, b_source, kind="chat", text=text, **extra)
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        if [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == mid]:
            self.assertEqual(200, self.gw.admit(a, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        return mid, job

    def column(self, mid, name):
        return self.gw.sql("SELECT %s FROM messages WHERE id=?" % name, (mid,))[0][name]


class BlobGatewayTests(BlobGatewayCase):
    def test_large_bodies_live_in_the_store_and_read_back_unchanged(self):
        a, wtoken, b_source, cid = self.friends()
        mid, job = self.claimed(a, wtoken, cid, b_source, [media("menu.png", BIG_PNG), media("menu.pdf", PDF)])
        files = job["untrusted_content"]["attachments"]
        self.assertEqual(BIG_PNG, base64.b64decode(files[0]["data"]))
        ref = self.column(mid, "content")
        key = B.parse_ref(ref)[0]
        stored = self.s3.objects["/%s/gw/%s" % (BUCKET, key)]
        self.assertIn(files[0]["data"], gzip.decompress(stored).decode())
        self.assertEqual(1, len(self.gw.sql("SELECT * FROM blobs WHERE key=?", (key,))))
        reply = {"text": "a photo back", "attachments": [media("dish.png", BIG_PNG)]}
        status, done = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                    {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                     "outcome": "completed", "reply": reply}, wtoken)
        self.assertEqual(200, status, done)
        if done["state"] == "review":
            self.assertTrue(self.column(mid, "draft").startswith(B.PREFIX))
            self.assertEqual(200, self.gw.approve_reply(a, mid)[0])
        self.assertTrue(self.column(mid, "result").startswith(B.PREFIX))
        view = self.gw.ok("GET", "/v1/messages/%s" % mid, None, b_source)
        self.assertEqual(BIG_PNG, base64.b64decode(view["result"]["attachments"][0]["data"]))
        self.assertEqual("the menu", self.gw.owner_view(cid)["messages"][0]["content"]["text"])
        # a duplicate finish compares with the stored body, read back from the store
        status, again = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                     {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                      "outcome": "completed", "reply": reply}, wtoken)
        self.assertEqual((200, True), (status, again.get("duplicate")), again)

    def test_small_bodies_stay_in_the_database(self):
        a, wtoken, b_source, cid = self.friends()
        mid, _ = self.claimed(a, wtoken, cid, b_source, None, text="just text")
        self.assertEqual("just text", json.loads(self.column(mid, "content"))["text"])

    def test_a_store_that_is_down_fails_the_request_and_never_stores_half(self):
        a, wtoken, b_source, cid = self.friends()
        before = len(self.gw.owner_view(cid)["messages"])
        self.s3.down = True
        status, obj = self.gw.send(cid, b_source, kind="chat", text="menu", attachments=[media("m.png", BIG_PNG)])
        self.assertEqual((503, "storage_unavailable"), (status, obj.get("error")), obj)
        self.s3.down = False
        self.assertEqual(before, len(self.gw.owner_view(cid)["messages"]))

    def test_an_altered_or_missing_body_is_never_served(self):
        self.gw.restart()               # an empty cache: reads go to the store
        a, wtoken, b_source, cid = self.friends()
        mid, _ = self.claimed(a, wtoken, cid, b_source, [media("menu.png", BIG_PNG)])
        self.gw.restart()
        path = "/%s/gw/%s" % (BUCKET, B.parse_ref(self.column(mid, "content"))[0])
        self.s3.objects[path] = gzip.compress(b'{"text":"not what was sent"}')
        status, obj = self.gw.call("GET", "/v1/messages/%s" % mid, None, b_source)
        self.assertEqual((503, "storage_unavailable"), (status, obj.get("error")), obj)
        del self.s3.objects[path]
        status, obj = self.gw.call("GET", "/v1/messages/%s" % mid, None, b_source)
        self.assertEqual(503, status, obj)

    def test_bodies_nobody_references_are_collected(self):
        a, wtoken, b_source, cid = self.friends()
        mid, _ = self.claimed(a, wtoken, cid, b_source, [media("menu.png", BIG_PNG)])
        key = B.parse_ref(self.column(mid, "content"))[0]
        orphan = B.new_key()
        self.s3.objects["/%s/gw/%s" % (BUCKET, orphan)] = b"x"
        db = self.gw.db()
        try:
            db.execute("INSERT INTO blobs(key, bytes, stored_bytes, created_ts) VALUES(?,1,1,0)", (orphan,))
            db.execute("UPDATE blobs SET created_ts=0")
        finally:
            db.close()
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        self.assertEqual(1, G.sweep_blobs(store, G.C.now_ts()))      # the orphan only: the message still refers to its body
        self.assertNotIn("/%s/gw/%s" % (BUCKET, orphan), self.s3.objects)
        self.assertIn("/%s/gw/%s" % (BUCKET, key), self.s3.objects)
        db = self.gw.db()
        try:
            db.execute("UPDATE messages SET content='{}', result=NULL, draft=NULL WHERE id=?", (mid,))
        finally:
            db.close()
        self.assertEqual(1, G.sweep_blobs(store, G.C.now_ts()))
        self.assertNotIn("/%s/gw/%s" % (BUCKET, key), self.s3.objects)
        self.assertEqual([], self.gw.sql("SELECT * FROM blobs WHERE key IN (?,?)", (key, orphan)))


class LocalStoreTests(unittest.TestCase):
    def test_a_local_directory_store(self):
        with tempfile.TemporaryDirectory(prefix="yobiru local blobs ") as tmp:
            gw = FriendsGateway(Path(tmp), limits=SPILL)
            private(gw.home / "blobs.json", json.dumps({"kind": "local"}))
            gw.start()
            try:
                a, a_source = gw.owned_bot("local-a")
                b, b_source = gw.owned_bot("local-b")
                gw.follow(b_source, a)
                gw.follow(a_source, b)
                cid = gw.conversation(a, b_source)
                status, sent = gw.send(cid, b_source, kind="chat", text="menu",
                                       attachments=[media("menu.png", BIG_PNG)])
                self.assertEqual(202, status, sent)
                ref = gw.sql("SELECT content FROM messages WHERE id=?", (sent["message_id"],))[0]["content"]
                key = B.parse_ref(ref)[0]
                self.assertTrue((gw.home / "blobs" / key[:2] / key).is_file())
                view = gw.ok("GET", "/v1/messages/%s" % sent["message_id"], None, b_source)
                self.assertEqual("menu", view["content"]["text"])
            finally:
                gw.stop()


class MigrationTests(unittest.TestCase):
    def test_schema_6_gets_the_blob_table_and_keeps_friend_presets(self):
        with tempfile.TemporaryDirectory(prefix="yobiru migration 6-7 ") as tmp:
            gw = FriendsGateway(Path(tmp)).start()
            try:
                taken = gw.bot("files-taken-back", policy={"offered": ["faq", "chat"],
                                                          "friend": ["faq", "chat"]})
            finally:
                gw.stop()
            db = gw.db()
            try:
                db.execute("DROP TABLE blobs")
                db.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
            finally:
                db.close()
            done = subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(gw.home),
                                   "migrate"], capture_output=True, text=True, env=plain_env(Path(tmp)), timeout=60)
            self.assertEqual(0, done.returncode, done.stderr)
            summary = json.loads(done.stdout)
            self.assertEqual(("8", "6"), (summary["schema"], summary["from_schema"]))
            self.assertNotIn("friend_files_bots", summary)
            pol = json.loads(gw.sql("SELECT policy FROM bots WHERE id=?", (taken,))[0]["policy"])
            self.assertNotIn("files", pol["friend"])
            self.assertEqual([], gw.sql("SELECT * FROM blobs"))


if __name__ == "__main__":
    unittest.main()
