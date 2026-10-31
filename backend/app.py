"""NAS expense register: authenticated HTTP API, SQLite and immutable originals.

Run `python -m backend.app --help`. No account is created automatically.
Production requires a configured HTTPS origin and secure session cookies.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import getpass
import hashlib
import hmac
import http.cookies
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIR_MODE = 0o777 if os.name == 'nt' else 0o700
MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL_FILES = 50 * 1024 * 1024
MAX_BODY = 72 * 1024 * 1024
MAX_ATTACHMENTS = 20
PASSWORD_ITERATIONS = 600_000
CATEGORIES = {'餐飲', '加油／交通', '一般採購', '軟體／訂閱', '辦公／文具', '設備', '租金／水電', '專業服務', '其他'}
DOC_TYPES = {'電子發票', '統一發票', '收據', '國外憑證', '其他證明'}
CURRENCIES = {'TWD', 'USD', 'JPY', 'EUR', 'CNY', 'HKD', 'GBP'}
BUSINESS_FIELDS = ('vendor', 'date', 'amount', 'currency', 'documentType', 'category', 'purpose', 'notes', 'invoiceNo')
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/store.js': ('store.js', 'text/javascript; charset=utf-8'),
    '/server-store.js': ('server-store.js', 'text/javascript; charset=utf-8'),
    '/cloud-store.js': ('cloud-store.js', 'text/javascript; charset=utf-8'),
    '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/assets/company-logo.jpg': ('assets/company-logo.jpg', 'image/jpeg'),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('manager','employee')),
 password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 csrf_token TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL,
 email TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_at ON login_attempts(at);
CREATE TABLE IF NOT EXISTS records (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0)
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(id),
 filename TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, object_path TEXT UNIQUE NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_record ON documents(record_id);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL REFERENCES records(id),
 actor_id TEXT NOT NULL REFERENCES users(id), actor_name TEXT NOT NULL,
 action TEXT NOT NULL, version INTEGER NOT NULL,
 old_payload TEXT, new_payload TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_record ON audit_events(record_id,id);
PRAGMA user_version=1;
"""


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def uuid_text(value, field='識別碼') -> str:
    try:
        result = str(uuid.UUID(str(value)))
        if str(value).lower() != result:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise APIError(400, f'{field}格式不正確') from None


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise APIError(400, '電子郵件格式不正確')
    value = email.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise APIError(400, '電子郵件格式不正確')
    return value


def password_hash(password: str) -> str:
    if not isinstance(password, str) or len(password) < 12 or len(password.encode('utf-8')) > 1024:
        raise ValueError('密碼至少需要 12 個字元，且不得超過 1024 bytes。')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password.encode('utf-8')) > 1024:
        return False
    try:
        algorithm, count, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(count))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


class Repository:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.evidence = self.data_dir / 'evidence'
        self.staging = self.data_dir / '.staging'
        for folder in (self.data_dir, self.evidence, self.staging):
            folder.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'expense-records.sqlite3'
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError('資料庫版本不受此程式支援，請勿覆寫既有資料。')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
        with contextlib.suppress(OSError):
            self.db_path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=20000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, email, name, role, password):
        email = normalize_email(email)
        if role not in ('manager', 'employee') or not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('請填寫有效的角色與顯示名稱。')
        encoded = password_hash(password)
        user_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,1,?)', (user_id, email, name.strip(), role, encoded, utc_now()))
        return user_id

    def user_change(self, email, *, password=None, role=None, active=None):
        email = normalize_email(email)
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            if not row:
                raise ValueError('找不到此帳號。')
            if password is not None:
                db.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash(password), row['id']))
            if role is not None:
                if role not in ('manager', 'employee'):
                    raise ValueError('角色不正確。')
                db.execute('UPDATE users SET role=? WHERE id=?', (role, row['id']))
            if active is not None:
                db.execute('UPDATE users SET active=? WHERE id=?', (int(active), row['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))

    def document_metadata(self, d):
        return {'id': d['id'], 'recordId': d['record_id'], 'filename': d['filename'], 'mime': d['mime'],
                'sizeBytes': d['size_bytes'], 'sha256': d['sha256']}

    def record_json(self, row, documents):
        return {**json.loads(row['payload']), 'id': row['id'], 'version': row['version'],
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'createdBy': row['created_by'],
                'history': [], 'documents': [self.document_metadata(d) for d in documents]}

    def all_records(self, db=None):
        if db is None:
            with self.connect() as connection:
                return self.all_records(connection)
        records = db.execute('SELECT * FROM records ORDER BY created_at DESC,id').fetchall()
        docs = db.execute('SELECT * FROM documents ORDER BY created_at,id').fetchall()
        grouped = {}
        for d in docs:
            grouped.setdefault(d['record_id'], []).append(d)
        return [self.record_json(r, grouped.get(r['id'], [])) for r in records]

    def file_path(self, row):
        # The generated path never uses the original filename or a request path.
        expected = f"evidence/{uuid_text(row['record_id'])}/{uuid_text(row['id'])}"
        if row['object_path'] != expected:
            raise APIError(500, '附件儲存位置驗證失敗')
        path = self.data_dir / expected
        if not path.resolve().is_relative_to(self.evidence.resolve()):
            raise APIError(500, '附件儲存位置不正確')
        if not path.is_file() or path.stat().st_size != row['size_bytes']:
            raise APIError(409, '原始附件遺失或不完整，請由管理者從 NAS 備份恢復')
        return path

    def audit_rows(self, record_id=None, db=None):
        if db is None:
            with self.connect() as connection:
                return self.audit_rows(record_id, connection)
        rows = db.execute('SELECT * FROM audit_events' + (' WHERE record_id=?' if record_id else '') + ' ORDER BY id', (record_id,) if record_id else ()).fetchall()
        return [{'id': r['id'], 'record_id': r['record_id'], 'actor': r['actor_name'], 'actorId': r['actor_id'],
                 'action': r['action'], 'version': r['version'], 'at': r['at'],
                 'old_payload': json.loads(r['old_payload']) if r['old_payload'] else None,
                 'new_payload': json.loads(r['new_payload'])} for r in rows]

    def save_record(self, body, user, record_id=None):
        if not isinstance(body, dict):
            raise APIError(400, '請求格式不正確')
        raw_record = body.get('record')
        payload = validate_record(raw_record)
        creating = record_id is None
        record_id = uuid_text(raw_record.get('id')) if creating else uuid_text(record_id)
        if not creating and raw_record.get('id') != record_id:
            raise APIError(400, '紀錄編號不一致')
        prepared = prepare_attachments(body.get('attachments', []))
        if creating and not prepared:
            raise APIError(400, '每筆支出至少需要一份原始附件')
        if not creating and (type(body.get('expectedVersion')) is not int or body['expectedVersion'] < 1):
            raise APIError(400, '需要有效的 expectedVersion')
        staged, moved = [], []
        try:
            for doc in prepared:
                temp = self.staging / secrets.token_hex(24)
                with temp.open('xb') as stream:
                    with contextlib.suppress(OSError):
                        temp.chmod(0o600)
                    stream.write(doc.pop('bytes'))
                    stream.flush()
                    os.fsync(stream.fileno())
                staged.append((doc, temp))
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current_user = db.execute('SELECT * FROM users WHERE id=? AND active=1', (user['id'],)).fetchone()
                if not current_user or (not creating and current_user['role'] != 'manager'):
                    raise APIError(403, '帳號權限已變更，請重新登入')
                old = db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
                if creating and old:
                    raise APIError(409, '此紀錄編號已存在，請重新整理以確認是否已送出')
                if not creating and not old:
                    raise APIError(404, '找不到紀錄')
                if not creating and old['version'] != body['expectedVersion']:
                    raise APIError(409, '這筆紀錄已被更新，請重新開啟後再修改')
                existing_docs = db.execute('SELECT * FROM documents WHERE record_id=?', (record_id,)).fetchall()
                timestamp = utc_now()
                old_payload = json.loads(old['payload']) if old else None
                if creating:
                    payload['status'] = 'pending'
                elif old_payload['status'] == 'reviewed':
                    if not payload['correctionReason']:
                        raise APIError(400, '修改已檢閱紀錄時必須填寫修改原因')
                    payload['status'] = 'pending'
                if payload['status'] == 'reviewed':
                    if not existing_docs and not prepared:
                        raise APIError(409, '附件不完整，無法標記已檢閱')
                    for d in existing_docs:
                        path = self.file_path(d)
                        if sha256_file(path) != d['sha256']:
                            raise APIError(409, '原始附件校驗失敗，無法完成檢閱')
                    payload['reviewedAt'] = timestamp
                    payload['reviewedBy'] = user['id']
                else:
                    payload['reviewedAt'] = None
                    payload['reviewedBy'] = None
                payload['uploadState'] = 'complete'
                version = old['version'] + 1 if old else 1
                serialized = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                if creating:
                    db.execute('INSERT INTO records VALUES (?,?,?,?,?,?)', (record_id, serialized, user['id'], timestamp, timestamp, version))
                else:
                    db.execute('UPDATE records SET payload=?,updated_at=?,version=? WHERE id=?', (serialized, timestamp, version, record_id))
                for doc, temp in staged:
                    relative = f"evidence/{record_id}/{doc['id']}"
                    destination = self.data_dir / relative
                    if destination.exists():
                        raise APIError(409, '附件編號已存在，原始檔案不允許覆寫')
                    db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)',
                               (doc['id'], record_id, doc['filename'], doc['mime'], doc['sizeBytes'], doc['sha256'], relative, user['id'], timestamp))
                    destination.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
                    temp.replace(destination)
                    moved.append(destination)
                    sync_directory(destination.parent)
                if staged:
                    sync_directory(self.evidence)
                action = 'INSERT' if creating else ('REVIEW' if payload['status'] == 'reviewed' else 'UPDATE')
                db.execute('INSERT INTO audit_events(record_id,actor_id,actor_name,action,version,old_payload,new_payload,at) VALUES (?,?,?,?,?,?,?,?)',
                           (record_id, user['id'], user['display_name'] or user['email'], action, version, old['payload'] if old else None, serialized, timestamp))
                new_row = db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()
                docs = db.execute('SELECT * FROM documents WHERE record_id=? ORDER BY created_at,id', (record_id,)).fetchall()
                result = self.record_json(new_row, docs)
            # SQLite commit completed. From here the originals must not be cleaned up.
            moved.clear()
            return result
        except sqlite3.IntegrityError:
            raise APIError(409, '紀錄或附件編號已存在，未覆寫既有資料') from None
        finally:
            for _, temp in staged:
                with contextlib.suppress(OSError):
                    temp.unlink()
            for path in moved:
                with contextlib.suppress(OSError):
                    path.unlink()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sync_directory(path):
    """Persist renamed directory entries where the operating system supports it."""
    if hasattr(os, 'O_DIRECTORY'):
        with contextlib.suppress(OSError):
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def validate_record(record):
    if not isinstance(record, dict):
        raise APIError(400, '支出紀錄格式不正確')
    payload = {}
    limits = {'vendor': 200, 'purpose': 1000, 'notes': 5000, 'invoiceNo': 100, 'correctionReason': 1000}
    for key, limit in limits.items():
        value = record.get(key, '')
        if not isinstance(value, str) or len(value) > limit or '\x00' in value:
            raise APIError(400, f'{key} 內容格式不正確或過長')
        payload[key] = value.strip()
    if not payload['vendor'] or not payload['purpose']:
        raise APIError(400, '商家與支出用途為必填')
    date = record.get('date', '')
    try:
        if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            raise ValueError()
        dt.date.fromisoformat(date)
    except ValueError:
        raise APIError(400, '憑證日期不正確') from None
    amount = record.get('amount', '')
    if not isinstance(amount, str) or not re.fullmatch(r'\d{1,12}(\.\d{1,2})?', amount) or not any(c in '123456789' for c in amount):
        raise APIError(400, '金額須大於零，最多 12 位整數與 2 位小數')
    for key, allowed in (('currency', CURRENCIES), ('category', CATEGORIES), ('documentType', DOC_TYPES), ('status', {'pending', 'reviewed'})):
        value = record.get(key, 'pending' if key == 'status' else '')
        if not isinstance(value, str) or value not in allowed:
            raise APIError(400, f'{key} 內容不正確')
        payload[key] = value
    payload.update(date=date, amount=amount)
    return payload


def prepare_attachments(attachments):
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise APIError(400, '每次最多上傳 20 份附件')
    prepared, identifiers, total = [], set(), 0
    for item in attachments:
        if not isinstance(item, dict):
            raise APIError(400, '附件格式不正確')
        doc_id = uuid_text(item.get('id', str(uuid.uuid4())), '附件識別碼')
        if doc_id in identifiers:
            raise APIError(400, '同一請求不可包含重複附件編號')
        identifiers.add(doc_id)
        filename = item.get('filename', '')
        if not isinstance(filename, str) or not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise APIError(400, '附件檔名不正確')
        mime = item.get('mime') or 'application/octet-stream'
        if not isinstance(mime, str) or len(mime) > 100 or not re.fullmatch(r'[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+', mime):
            raise APIError(400, '附件 MIME 格式不正確')
        encoded = item.get('base64')
        if not isinstance(encoded, str):
            raise APIError(400, '附件需要 base64 原始資料')
        if len(encoded) > (MAX_FILE + 2) // 3 * 4:
            raise APIError(413, '單份附件不得超過 25 MB')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise APIError(400, '附件 base64 資料不正確') from None
        total += len(data)
        if not data:
            raise APIError(400, '不可上傳空白附件')
        if len(data) > MAX_FILE or total > MAX_TOTAL_FILES:
            raise APIError(413, '單份附件上限 25 MB，每次上傳合計上限 50 MB')
        digest = hashlib.sha256(data).hexdigest()
        if item.get('sha256') and item['sha256'] != digest:
            raise APIError(400, '附件 SHA-256 校驗失敗，未儲存紀錄')
        prepared.append({'id': doc_id, 'filename': filename, 'mime': mime, 'sizeBytes': len(data), 'sha256': digest, 'bytes': data})
    return prepared


class Settings:
    def __init__(self, data_dir=None, *, origin=None, dev=None, host=None, port=None):
        self.data_dir = Path(data_dir or os.environ.get('APP_DATA_DIR', ROOT / 'nas-data'))
        self.dev = os.environ.get('APP_DEV_LOCALHOST', '0') == '1' if dev is None else dev
        self.host = host or os.environ.get('APP_HOST', '127.0.0.1' if self.dev else '0.0.0.0')
        self.port = int(port if port is not None else os.environ.get('APP_PORT', '8080'))
        self.origin = (origin if origin is not None else os.environ.get('APP_PUBLIC_ORIGIN', '')).rstrip('/')
        parsed = urlsplit(self.origin)
        if not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('APP_PUBLIC_ORIGIN 必須是完整且不含路徑的公開網址。')
        try:
            configured_port = parsed.port
        except ValueError:
            raise ValueError('APP_PUBLIC_ORIGIN 的連接埠不正確。') from None
        if self.dev:
            if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or self.host not in ('localhost', '127.0.0.1', '::1'):
                raise ValueError('APP_DEV_LOCALHOST=1 只允許 HTTP localhost 網址與本機綁定。')
        elif parsed.scheme != 'https':
            raise ValueError('正式模式需要 HTTPS APP_PUBLIC_ORIGIN；不允許不安全的登入 Cookie。')
        hostname = parsed.hostname.lower()
        if ':' in hostname:
            hostname = f'[{hostname}]'
        default_port = 443 if parsed.scheme == 'https' else 80
        self.origin = f'{parsed.scheme}://{hostname}' + (f':{configured_port}' if configured_port is not None and configured_port != default_port else '')
        self.cookie_name = 'expense_session_dev' if self.dev else '__Host-expense_session'
        self.idle_seconds = int(os.environ.get('APP_SESSION_IDLE_SECONDS', '1800'))
        self.max_seconds = int(os.environ.get('APP_SESSION_MAX_SECONDS', '43200'))
        if self.idle_seconds < 60 or self.max_seconds < self.idle_seconds or self.max_seconds > 7 * 86400:
            raise ValueError('Session 時間設定不正確。')


class ExpenseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings):
        self.settings = settings
        self.repo = Repository(settings.data_dir)
        self.slots = threading.BoundedSemaphore(8)
        self.dummy_password = password_hash(secrets.token_urlsafe(32))
        super().__init__((settings.host, settings.port), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ExpenseRecords/1.0'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def repo(self):
        return self.server.repo

    @property
    def settings(self):
        return self.server.settings

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Permissions-Policy', 'camera=(self), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-src blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Cache-Control', 'no-store')
        if not self.settings.dev:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        super().end_headers()

    def send_json(self, status, value=None, *, cookie=None):
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') if value is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def cookie(self, token='', clear=False):
        secure = '' if self.settings.dev else '; Secure'
        age = 0 if clear else self.settings.max_seconds
        return f'{self.settings.cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}'

    def read_json(self, maximum=MAX_BODY):
        if self.headers.get('Transfer-Encoding'):
            raise APIError(411, '請提供 Content-Length，不接受分塊請求')
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            raise APIError(400, 'Content-Length 不正確') from None
        if length < 0:
            raise APIError(411, '需要 Content-Length')
        if length > maximum:
            raise APIError(413, '請求資料過大，請減少附件總大小')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise APIError(415, '請使用 application/json')
        raw = self.rfile.read(length)
        self.body_consumed = len(raw)
        if len(raw) != length:
            raise APIError(400, '請求資料不完整')
        try:
            def reject_constant(_):
                raise ValueError()
            value = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, 'JSON 格式不正確') from None
        if not isinstance(value, dict):
            raise APIError(400, '請求必須是 JSON 物件')
        return value

    def check_origin(self):
        if self.headers.get('Origin') != self.settings.origin or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise APIError(403, '請從已設定的正式網址操作，來源驗證失敗')

    def session(self, manager=False, csrf=False):
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise APIError(403, '不允許跨網站存取')
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
            token = jar[self.settings.cookie_name].value
            if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', token):
                raise KeyError()
        except (http.cookies.CookieError, KeyError):
            raise APIError(401, '請先登入工作空間') from None
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        current = time.time()
        with self.repo.connect() as db:
            row = db.execute('SELECT sessions.*,users.email,users.display_name,users.role,users.active FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=?', (token_hash,)).fetchone()
            invalid = not row or not row['active'] or current-row['last_seen'] > self.settings.idle_seconds or current-row['created_at'] > self.settings.max_seconds
            if invalid:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash,))
            elif current-row['last_seen'] > 60:
                db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (current, token_hash))
        if invalid:
            raise APIError(401, '登入已到期，請重新登入')
        if manager and row['role'] != 'manager':
            raise APIError(403, '此功能僅供管理者使用')
        if csrf:
            self.check_origin()
            supplied = self.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(supplied.encode('utf-8'), row['csrf_token'].encode('utf-8')):
                raise APIError(403, '操作驗證已失效，請重新整理或登入')
        self.current_session_hash = token_hash
        return {'id': row['user_id'], 'email': row['email'], 'display_name': row['display_name'], 'role': row['role']}, row['csrf_token']

    def login(self):
        self.check_origin()
        body = self.read_json(16 * 1024)
        try:
            email = normalize_email(body.get('email', ''))
        except APIError:
            email = 'invalid'
        password = body.get('password', '')
        current, ip = time.time(), self.client_address[0]
        with self.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_attempts WHERE at<?', (current-900,))
            by_ip = db.execute('SELECT COUNT(*) FROM login_attempts WHERE ip=?', (ip,)).fetchone()[0]
            by_email = db.execute('SELECT COUNT(*) FROM login_attempts WHERE email=?', (email,)).fetchone()[0]
            if by_ip >= 30 or by_email >= 5:
                raise APIError(429, '登入嘗試過多，請等待 15 分鐘後再試')
            # Reserve an attempt before password work, so concurrent requests cannot bypass limits.
            db.execute('INSERT INTO login_attempts(ip,email,at) VALUES (?,?,?)', (ip, email, current))
            attempt_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        encoded = row['password_hash'] if row else self.server.dummy_password
        valid = verify_password(password, encoded)
        if not row or not row['active'] or not valid:
            raise APIError(401, '登入失敗，請確認帳號與密碼')
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
            if not fresh or not fresh['active'] or not hmac.compare_digest(fresh['password_hash'], encoded):
                raise APIError(401, '帳號已變更，請重新登入')
            row = fresh
            db.execute('DELETE FROM login_attempts WHERE id=?', (attempt_id,))
            db.execute('DELETE FROM sessions WHERE created_at<? OR last_seen<?', (current-self.settings.max_seconds, current-self.settings.idle_seconds))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row['id'], csrf_token, current, current))
        user = {k: row[k] for k in ('id', 'email', 'display_name', 'role')}
        self.send_json(200, {'user': user, 'csrfToken': csrf_token}, cookie=self.cookie(token))

    def send_file(self, path, mime, filename=None):
        with path.open('rb') as stream:
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(path.stat().st_size))
            if filename:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile, 256 * 1024)

    def backup(self):
        with self.repo.connect() as db:
            db.execute('BEGIN')
            records = self.repo.all_records(db)
            documents = db.execute('SELECT * FROM documents ORDER BY record_id,id').fetchall()
            audit = self.repo.audit_rows(db=db)
        paths = [(d, self.repo.file_path(d)) for d in documents]
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Disposition', 'attachment; filename="expense-records-backup.json"')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        prefix = {'format': 'if-expense-backup', 'schemaVersion': 1, 'exportedAt': utc_now(), 'mode': 'server', 'records': records, 'audit_events': audit}
        self.wfile.write(json.dumps(prefix, ensure_ascii=False, separators=(',', ':'))[:-1].encode('utf-8') + b',"documents":[')
        for index, (doc, path) in enumerate(paths):
            if index:
                self.wfile.write(b',')
            meta = self.repo.document_metadata(doc)
            self.wfile.write(json.dumps(meta, ensure_ascii=False, separators=(',', ':'))[:-1].encode('utf-8') + b',"base64":"')
            with path.open('rb') as stream:
                # Multiple-of-three blocks concatenate into one valid Base64 string.
                for chunk in iter(lambda: stream.read(57 * 1024), b''):
                    self.wfile.write(base64.b64encode(chunk))
            self.wfile.write(b'"}')
        self.wfile.write(b']}')

    def dispatch(self):
        path = urlsplit(self.path).path
        if self.command == 'GET' and path == '/healthz':
            with self.repo.connect() as db:
                db.execute('SELECT 1').fetchone()
            return self.send_json(200, {'status': 'ok'})
        if self.command == 'GET' and path == '/api/config':
            return self.send_json(200, {'mode': 'server', 'product': '支出憑證紀錄系統', 'maxFileBytes': MAX_FILE, 'maxTotalFileBytes': MAX_TOTAL_FILES, 'maxAttachments': MAX_ATTACHMENTS})
        if self.command == 'POST' and path == '/api/login':
            return self.login()
        if self.command == 'GET' and path == '/api/session':
            user, token = self.session()
            return self.send_json(200, {'user': user, 'csrfToken': token})
        if self.command == 'POST' and path == '/api/logout':
            self.session(csrf=True)
            with self.repo.connect() as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (self.current_session_hash,))
            return self.send_json(204, cookie=self.cookie(clear=True))
        if path == '/api/records' and self.command == 'GET':
            self.session(manager=True)
            return self.send_json(200, self.repo.all_records())
        if path == '/api/records' and self.command == 'POST':
            user, _ = self.session(csrf=True)
            result = self.repo.save_record(self.read_json(), user)
            if user['role'] == 'employee':
                # Only this request's confirmation, never a listing or historical finance response.
                result = {k: result[k] for k in ('id', 'version', 'createdAt', 'updatedAt', 'uploadState', 'status')}
            return self.send_json(201, result)
        if self.command == 'PATCH' and re.fullmatch(r'/api/records/[^/]+', path):
            user, _ = self.session(manager=True, csrf=True)
            return self.send_json(200, self.repo.save_record(self.read_json(), user, path.rsplit('/', 1)[1]))
        if self.command == 'GET' and re.fullmatch(r'/api/documents/[^/]+', path):
            self.session(manager=True)
            doc_id = uuid_text(path.rsplit('/', 1)[1])
            with self.repo.connect() as db:
                doc = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not doc:
                raise APIError(404, '找不到附件')
            actual = self.repo.file_path(doc)
            if sha256_file(actual) != doc['sha256']:
                raise APIError(409, '附件校驗失敗，請由管理者從 NAS 備份恢復')
            return self.send_file(actual, doc['mime'], doc['filename'])
        if self.command == 'GET' and re.fullmatch(r'/api/audit/[^/]+', path):
            self.session(manager=True)
            record_id = uuid_text(path.rsplit('/', 1)[1])
            return self.send_json(200, self.repo.audit_rows(record_id))
        if self.command == 'GET' and path == '/api/backup':
            self.session(manager=True)
            return self.backup()
        if self.command == 'GET' and path in STATIC:
            filename, mime = STATIC[path]
            actual = ROOT / filename
            if actual.is_file():
                return self.send_file(actual, mime)
        raise APIError(404, '找不到此資源')

    def handle_request(self):
        self.body_consumed = 0
        try:
            self.dispatch()
        except APIError as error:
            # Closing a TCP socket with unread request bytes can reset the connection
            # before the client receives the error (notably on Windows). Drain a bounded
            # already-sent body without accepting or processing it; never wait indefinitely.
            try:
                remaining = int(self.headers.get('Content-Length', '0')) - self.body_consumed
                deadline = time.monotonic() + 2
                if 0 < remaining <= MAX_BODY:
                    while remaining > 0 and time.monotonic() < deadline:
                        self.connection.settimeout(max(0.05, min(0.5, deadline-time.monotonic())))
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
            except (OSError, ValueError):
                pass
            self.connection.settimeout(30)
            self.send_json(error.status, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            logging.exception('Request failed: %s %s', self.command, urlsplit(self.path).path)
            with contextlib.suppress(OSError):
                self.send_json(500, {'error': '伺服器未完成操作，請確認 NAS 儲存空間與伺服器紀錄'})

    do_GET = handle_request
    do_POST = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request
    do_PUT = handle_request

    def log_message(self, fmt, *args):
        logging.info('%s %s', self.client_address[0], fmt % args)


def get_password(stdin=False):
    if stdin:
        value = sys.stdin.readline(2049).rstrip('\r\n')
        if not value:
            raise ValueError('標準輸入沒有提供密碼。')
        return value
    first = getpass.getpass('新密碼（至少 12 字元）：')
    second = getpass.getpass('再次輸入密碼：')
    if first != second:
        raise ValueError('兩次密碼不一致。')
    return first


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='支出憑證 NAS 服務與帳號管理（不建立預設帳密）')
    parser.add_argument('--data-dir', help='資料目錄；預設 APP_DATA_DIR 或 ./nas-data')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve', help='啟動 HTTP 服務，由 NAS HTTPS 反向代理提供正式連線')
    create = commands.add_parser('create-user', help='新增受邀帳號')
    create.add_argument('--email', required=True)
    create.add_argument('--name', required=True)
    create.add_argument('--role', required=True, choices=['manager', 'employee'])
    create.add_argument('--password-stdin', action='store_true')
    reset = commands.add_parser('reset-password', help='重設密碼並撤銷所有既有 Session')
    reset.add_argument('--email', required=True)
    reset.add_argument('--password-stdin', action='store_true')
    commands.add_parser('list-users', help='列出帳號，不顯示密碼或密碼雜湊')
    role = commands.add_parser('set-role', help='修改角色並撤銷所有既有 Session')
    role.add_argument('--email', required=True)
    role.add_argument('--role', required=True, choices=['manager', 'employee'])
    for name in ('disable-user', 'enable-user'):
        command = commands.add_parser(name)
        command.add_argument('--email', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            settings = Settings(args.data_dir)
            logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
            server = ExpenseServer(settings)
            logging.info('Listening on %s:%s for origin %s; mode=%s', settings.host, server.server_port, settings.origin, 'localhost-development' if settings.dev else 'production-secure-cookie')
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        repository = Repository(Path(args.data_dir or os.environ.get('APP_DATA_DIR', ROOT / 'nas-data')))
        if args.command == 'create-user':
            identifier = repository.create_user(args.email, args.name, args.role, get_password(args.password_stdin))
            print(f'已新增帳號 {normalize_email(args.email)}（{args.role}），ID={identifier}')
        elif args.command == 'reset-password':
            repository.user_change(args.email, password=get_password(args.password_stdin))
            print('密碼已更新，既有登入已全部撤銷。')
        elif args.command == 'set-role':
            repository.user_change(args.email, role=args.role)
            print('角色已更新，既有登入已全部撤銷。')
        elif args.command in ('disable-user', 'enable-user'):
            repository.user_change(args.email, active=args.command == 'enable-user')
            print('帳號狀態已更新，既有登入已全部撤銷。')
        elif args.command == 'list-users':
            with repository.connect() as db:
                for row in db.execute('SELECT id,email,display_name,role,active FROM users ORDER BY email'):
                    print(json.dumps(dict(row), ensure_ascii=False))
        return 0
    except (ValueError, APIError, sqlite3.IntegrityError) as error:
        print(f'操作失敗：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
