"""Real-HTTP integration tests against a temporary SQLite/file directory.

Run: python -m unittest backend.test_backend -v
No network outside loopback; no production records or accounts are touched.
"""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import threading
import time
import unittest
import uuid

from backend.app import ExpenseServer, Handler, Settings, APIError, ROOT

PASSWORD = 'Temporary-test-password-2026!'


class Client:
    def __init__(self, case):
        self.case = case
        self.cookie = ''
        self.csrf = ''

    def request(self, method, path, body=None, *, csrf=True, origin=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.case.server.server_port, timeout=20)
        request_headers = {}
        if self.cookie:
            request_headers['Cookie'] = self.cookie
        if origin:
            request_headers['Origin'] = self.case.settings.origin
        if csrf and self.csrf:
            request_headers['X-CSRF-Token'] = self.csrf
        raw = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        conn.request(method, path, body=raw, headers=request_headers)
        response = conn.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        data = response.read()
        conn.close()
        if response_headers.get('Content-Type', '').startswith('application/json') and data:
            data = json.loads(data)
        return status, data, response_headers

    def login(self, email, password=PASSWORD):
        status, data, headers = self.request('POST', '/api/login', {'email': email, 'password': password})
        if status == 200:
            self.cookie = headers['Set-Cookie'].split(';')[0]
            self.csrf = data['csrfToken']
        return status, data, headers


class BackendIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = ROOT / '.test-data' / ('backend-' + uuid.uuid4().hex)
        directory.mkdir(parents=True)
        cls.temp = type('TestDirectory', (), {'name': str(directory)})()
        cls.settings = Settings(directory, origin='http://127.0.0.1:0', dev=True, host='127.0.0.1', port=0)
        cls.server = ExpenseServer(cls.settings)
        cls.settings.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.server.repo.create_user('manager@example.test', '測試管理者', 'manager', PASSWORD)
        cls.server.repo.create_user('employee@example.test', '測試同仁', 'employee', PASSWORD)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        target = Path(cls.temp.name).resolve()
        assert target.is_relative_to((ROOT / '.test-data').resolve()) and target.name.startswith('backend-')
        shutil.rmtree(target)

    def client(self, role='manager'):
        client = Client(self)
        self.assertEqual(client.login(f'{role}@example.test')[0], 200)
        return client

    def record(self, **changes):
        return {'id': str(uuid.uuid4()), 'vendor': '測試商家', 'date': '2026-10-31', 'amount': '1250.50',
                'currency': 'TWD', 'category': '一般採購', 'documentType': '收據', 'purpose': '測試工作用途',
                'notes': '', 'invoiceNo': 'AB12345678', 'status': 'pending', **changes}

    def attachment(self, content=b'%PDF-1.4\nTEST ORIGINAL\n', **changes):
        return {'id': str(uuid.uuid4()), 'filename': '原始憑證.pdf', 'mime': 'application/pdf',
                'sha256': hashlib.sha256(content).hexdigest(), 'base64': base64.b64encode(content).decode(), **changes}

    def create(self, client=None, record=None, attachment=None):
        client = client or self.client()
        record = record or self.record()
        attachment = attachment or self.attachment()
        status, result, _ = client.request('POST', '/api/records', {'record': record, 'attachments': [attachment]})
        self.assertEqual(status, 201, result)
        return client, result, attachment

    def test_01_no_default_accounts_anonymous_denied_and_private_files_not_served(self):
        client = Client(self)
        for path in ['/api/session', '/api/records', '/api/backup', f'/api/documents/{uuid.uuid4()}']:
            self.assertEqual(client.request('GET', path)[0], 401)
        for path in ['/.env', '/.git/config', '/nas-data/expense-records.sqlite3', '/backend/app.py', '/../README.md']:
            self.assertEqual(client.request('GET', path)[0], 404)
        self.assertEqual(client.request('GET', '/healthz')[0], 200)
        status, config, _ = client.request('GET', '/api/config')
        self.assertEqual((status, config['mode']), (200, 'server'))
        self.assertEqual(client.request('GET', '/')[0], 200)

    def test_02_employee_upload_only_even_for_own_record(self):
        employee = self.client('employee')
        fake = self.record(status='reviewed', createdBy='fake', history=[{'action': 'forged'}])
        _, receipt, attachment = self.create(employee, fake)
        self.assertEqual(receipt['status'], 'pending')
        self.assertNotIn('documents', receipt)
        self.assertNotIn('vendor', receipt)
        for path in ['/api/records', '/api/backup', f"/api/audit/{receipt['id']}", f"/api/documents/{attachment['id']}"]:
            self.assertEqual(employee.request('GET', path)[0], 403)
        self.assertEqual(employee.request('PATCH', f"/api/records/{receipt['id']}", {'record': fake, 'expectedVersion': 1})[0], 403)

    def test_03_original_bytes_hash_and_filename_never_control_storage_path(self):
        original = b'preserve all original bytes\x00\xff\xfe'
        manager, result, attached = self.create(attachment=self.attachment(original, filename='../../outside-danger.pdf'))
        status, data, headers = manager.request('GET', f"/api/documents/{attached['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data, original)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(result['documents'][0]['sha256'], hashlib.sha256(original).hexdigest())
        self.assertTrue((Path(self.temp.name)/'evidence'/result['id']/attached['id']).is_file())
        self.assertFalse((Path(self.temp.name)/'outside-danger.pdf').exists())

    def test_04_optimistic_concurrency_and_reviewed_correction_history(self):
        manager, result, _ = self.create()
        patch = {**result, 'status': 'reviewed', 'reviewedBy': 'forged'}
        status, reviewed, _ = manager.request('PATCH', f"/api/records/{result['id']}", {'record': patch, 'expectedVersion': 1, 'attachments': []})
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed['version'], 2)
        self.assertEqual(reviewed['status'], 'reviewed')
        self.assertNotEqual(reviewed['reviewedBy'], 'forged')
        self.assertEqual(manager.request('PATCH', f"/api/records/{result['id']}", {'record': patch, 'expectedVersion': 1})[0], 409)
        self.assertEqual(manager.request('PATCH', f"/api/records/{result['id']}", {'record': reviewed, 'expectedVersion': 2})[0], 400)
        corrected = {**reviewed, 'amount': '1500', 'correctionReason': '依憑證更正金額'}
        status, saved, _ = manager.request('PATCH', f"/api/records/{result['id']}", {'record': corrected, 'expectedVersion': 2})
        self.assertEqual(status, 200, saved)
        self.assertEqual((saved['status'], saved['version']), ('pending', 3))
        self.assertIsNone(saved['reviewedAt'])
        status, audit, _ = manager.request('GET', f"/api/audit/{result['id']}")
        self.assertEqual([a['version'] for a in audit], [1, 2, 3])
        self.assertEqual(audit[-1]['old_payload']['amount'], '1250.50')
        self.assertEqual(audit[-1]['new_payload']['amount'], '1500')

    def test_05_invalid_original_does_not_create_partial_financial_record(self):
        manager = self.client()
        for attachment in [self.attachment(sha256='0'*64), self.attachment(b''), {'id': str(uuid.uuid4()), 'filename': 'x.pdf', 'base64': '!invalid!'}]:
            record = self.record()
            self.assertEqual(manager.request('POST', '/api/records', {'record': record, 'attachments': [attachment]})[0], 400)
            with self.server.repo.connect() as db:
                self.assertIsNone(db.execute('SELECT id FROM records WHERE id=?', (record['id'],)).fetchone())
        self.assertEqual(manager.request('POST', '/api/records', {'record': self.record(), 'attachments': []})[0], 400)

    def test_06_transaction_rolls_back_files_and_rows_on_duplicate_evidence_id(self):
        manager, _, existing = self.create()
        new_record = self.record()
        first = self.attachment(b'FIRST NEW FILE')
        status, _, _ = manager.request('POST', '/api/records', {'record': new_record, 'attachments': [first, existing]})
        self.assertEqual(status, 409)
        with self.server.repo.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM records WHERE id=?', (new_record['id'],)).fetchone())
            self.assertIsNone(db.execute('SELECT id FROM documents WHERE id=?', (first['id'],)).fetchone())
        self.assertFalse((Path(self.temp.name)/'evidence'/new_record['id']/first['id']).exists())
        self.assertEqual(manager.request('GET', f"/api/documents/{existing['id']}")[0], 200)

    def test_07_csrf_and_origin_are_required(self):
        manager = self.client()
        body = {'record': self.record(), 'attachments': [self.attachment()]}
        self.assertEqual(manager.request('POST', '/api/records', body, csrf=False)[0], 403)
        self.assertEqual(manager.request('POST', '/api/records', body, origin=False)[0], 403)
        self.assertEqual(manager.request('POST', '/api/records', body, headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(manager.request('GET', '/api/records', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(Client(self).request('POST', '/api/login', {'email': 'manager@example.test', 'password': PASSWORD}, origin=False)[0], 403)

    def test_08_backup_is_complete_and_contains_no_authentication_material(self):
        original = b'backup-original-' + bytes(range(256))*300
        manager, result, attachment = self.create(attachment=self.attachment(original))
        status, backup, _ = manager.request('GET', '/api/backup')
        self.assertEqual(status, 200)
        self.assertEqual((backup['format'], backup['schemaVersion'], backup['mode']), ('if-expense-backup', 1, 'server'))
        doc = next(d for d in backup['documents'] if d['id'] == attachment['id'])
        self.assertEqual(base64.b64decode(doc['base64']), original)
        self.assertEqual(doc['sha256'], hashlib.sha256(original).hexdigest())
        self.assertTrue(any(a['record_id'] == result['id'] for a in backup['audit_events']))
        encoded = json.dumps(backup)
        self.assertNotIn('password_hash', encoded)
        self.assertNotIn('token_hash', encoded)
        self.assertNotIn(PASSWORD, encoded)

    def test_09_reset_password_and_role_change_revoke_live_sessions(self):
        email = f'change-{uuid.uuid4().hex}@example.test'
        self.server.repo.create_user(email, '臨時同仁', 'employee', PASSWORD)
        client = Client(self)
        self.assertEqual(client.login(email)[0], 200)
        self.server.repo.user_change(email, password='Replacement-test-password-2026!')
        self.assertEqual(client.request('GET', '/api/session')[0], 401)
        self.assertEqual(client.login(email, 'Replacement-test-password-2026!')[0], 200)
        self.server.repo.user_change(email, role='manager')
        self.assertEqual(client.request('GET', '/api/session')[0], 401)

    def test_10_passwords_and_session_tokens_are_hashed_at_rest(self):
        manager = self.client()
        raw_token = manager.cookie.split('=', 1)[1]
        with self.server.repo.connect() as db:
            hashes = [r['password_hash'] for r in db.execute('SELECT password_hash FROM users')]
            sessions = [r['token_hash'] for r in db.execute('SELECT token_hash FROM sessions')]
        self.assertTrue(all(h.startswith('pbkdf2_sha256$600000$') for h in hashes))
        self.assertNotIn(raw_token, sessions)
        self.assertIn(hashlib.sha256(raw_token.encode()).hexdigest(), sessions)
        _, _, headers = manager.login('manager@example.test')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def test_11_login_throttle_persists_in_database(self):
        client = Client(self)
        email = 'throttled@example.test'
        for _ in range(5):
            self.assertEqual(client.login(email, 'wrong password')[0], 401)
        self.assertEqual(client.login(email, 'wrong password')[0], 429)
        with self.server.repo.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM login_attempts WHERE email=?', (email,)).fetchone()[0], 5)

    def test_12_incomplete_or_corrupted_original_cannot_be_reviewed(self):
        manager, result, attachment = self.create()
        path = Path(self.temp.name)/'evidence'/result['id']/attachment['id']
        original = path.read_bytes()
        path.write_bytes(b'X'*len(original))
        try:
            self.assertEqual(manager.request('GET', f"/api/documents/{attachment['id']}")[0], 409)
            self.assertEqual(manager.request('PATCH', f"/api/records/{result['id']}", {'record': {**result, 'status': 'reviewed'}, 'expectedVersion': 1})[0], 409)
        finally:
            path.write_bytes(original)

    def test_13_server_validation_rejects_bad_date_amount_currency_and_tax_fields(self):
        manager = self.client()
        for changes in [{'date': '2026-02-30'}, {'amount': '-1'}, {'amount': '1e6'}, {'amount': '12.345'}, {'currency': 'OTHER'}, {'purpose': ''}, {'category': '<script>'}]:
            self.assertEqual(manager.request('POST', '/api/records', {'record': self.record(**changes), 'attachments': [self.attachment()]})[0], 400)

    def test_14_secure_cookie_settings_refuse_insecure_production(self):
        with self.assertRaises(ValueError):
            Settings(self.temp.name, origin='http://nas.example.com', dev=False)
        with self.assertRaises(ValueError):
            Settings(self.temp.name, origin='http://nas.example.com', dev=True, host='127.0.0.1')
        production = Settings(self.temp.name, origin='https://expenses.example.com', dev=False)
        proxy = type('Proxy', (), {'settings': production})()
        header = Handler.cookie(proxy, 'test')
        self.assertIn('__Host-expense_session', header)
        self.assertIn('; Secure', header)

    def test_15_restart_preserves_accounts_records_originals_and_sessions(self):
        manager, result, attached = self.create()
        before = manager.request('GET', f"/api/documents/{attached['id']}")[1]
        old_port = self.server.server_port
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        type(self).settings.port = old_port
        type(self).server = ExpenseServer(self.settings)
        type(self).thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(manager.request('GET', '/api/session')[0], 200)
        self.assertEqual(manager.request('GET', f"/api/documents/{attached['id']}")[1], before)
        status, rows, _ = manager.request('GET', '/api/records')
        self.assertEqual(status, 200)
        self.assertTrue(any(r['id'] == result['id'] for r in rows))

    def test_16_logout_and_idle_expiry_revoke_server_session(self):
        manager = self.client()
        self.assertEqual(manager.request('POST', '/api/logout')[0], 204)
        self.assertEqual(manager.request('GET', '/api/session')[0], 401)
        self.assertEqual(manager.login('manager@example.test')[0], 200)
        token_hash = hashlib.sha256(manager.cookie.split('=', 1)[1].encode()).hexdigest()
        with self.server.repo.connect() as db:
            db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (time.time()-self.settings.idle_seconds-1, token_hash))
        self.assertEqual(manager.request('GET', '/api/session')[0], 401)
        with self.server.repo.connect() as db:
            self.assertIsNone(db.execute('SELECT token_hash FROM sessions WHERE token_hash=?', (token_hash,)).fetchone())


if __name__ == '__main__':
    unittest.main()
