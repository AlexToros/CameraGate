"""PostgreSQL persistence. All mutations are serialized, including bootstrap/invites.

ACL snapshots use four bulk queries, never one query per camera or frame.
Only the service layer can decrypt upstream credentials.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from cryptography.fernet import Fernet
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, label, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise Problem('Проверьте поле «%s»' % label)
    return value.strip()


def login_name(value):
    value = text(value, 'Логин', 64).lower()
    if not re.fullmatch(r'[a-z0-9._-]{3,64}', value):
        raise Problem('Логин: от 3 до 64 латинских букв, цифр, точек, дефисов или подчёркиваний')
    return value


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise Problem('Пароль должен содержать от 12 до 128 символов')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return 'pbkdf2_sha256$600000$%s$%s' % (salt, digest)


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    _, rounds, salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(actual, expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def ids(value):
    if not isinstance(value, list) or len(value) > 512 or any(type(x) is not int or x < 1 for x in value):
        raise Problem('Неверный список доступа')
    return sorted(set(value))


def profile(data):
    role = data.get('role')
    if role not in ('admin', 'user'):
        raise Problem('Выберите роль')
    return dict(first_name=text(data.get('first_name'), 'Имя'),
                last_name=text(data.get('last_name'), 'Фамилия'), role=role,
                camera_ids=ids(data.get('camera_ids', [])), group_ids=ids(data.get('group_ids', [])))


def friendly_name(source, channel):
    name = source.strip()
    match = re.fullmatch(r'(?:dvor|двор)[ /_-]*(\d+)[pп]?[ /_-]*(\d+)?', name, re.I)
    if match:
        return 'Двор, подъезд %s%s' % (match[1], ', камера '+match[2] if match[2] else '')
    return name or 'Камера %s' % channel


class Store:
    def __init__(self, dsn, data_dir):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = self.directory / 'secret.key'
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as key_file:
                key_file.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.lock = threading.RLock()
        self.pool = ConnectionPool(dsn, min_size=1, max_size=4, timeout=10,
                                   kwargs={'row_factory': dict_row}, open=True)
        self.pool.wait(timeout=15)
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))
        with self.pool.connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(802701)')
            conn.execute((ROOT / 'schema.sql').read_text(), prepare=False)
        self.refresh()

    def close(self):
        self.pool.close()

    def _snapshot(self, conn):
        users = conn.execute('SELECT id,login,first_name,last_name,role,blocked,camera_ids,group_ids FROM users ORDER BY id').fetchall()
        cameras = conn.execute('SELECT id,display_name AS name,active FROM cameras ORDER BY id').fetchall()
        groups = conn.execute('SELECT * FROM camera_groups ORDER BY id').fetchall()
        sessions = conn.execute('SELECT token_hash,user_id,csrf,extract(epoch FROM expires_at) AS expires FROM sessions WHERE expires_at>now()').fetchall()
        camera_map = {c['id']: c for c in cameras if c['active']}
        group_map = {g['id']: g for g in groups}
        all_mask = sum(1 << cid for cid in camera_map)
        masks = {g['id']: all_mask if g['builtin'] else sum(1 << cid for cid in g['camera_ids'] if cid in camera_map) for g in groups}
        user_map = {}
        for user in users:
            mask = sum(1 << cid for cid in user['camera_ids'] if cid in camera_map)
            for gid in user['group_ids']:
                mask |= masks.get(gid, 0)
            user_map[user['id']] = {**user, 'mask': all_mask if user['role'] == 'admin' else mask}
        return dict(users=user_map, cameras=camera_map, groups=group_map,
                    sessions={s['token_hash']: s for s in sessions})

    def refresh(self):
        with self.lock, self.pool.connection() as conn:
            snapshot = self._snapshot(conn)
            self.snapshot = snapshot

    def mutate(self, action):
        with self.lock:
            try:
                with self.pool.connection() as conn:
                    conn.execute('SELECT pg_advisory_xact_lock(802701)')
                    result = action(conn)
                    snapshot = self._snapshot(conn)
                self.snapshot = snapshot
                return result
            except psycopg.errors.UniqueViolation:
                raise Problem('Такой логин или название уже существует', 409) from None

    def user(self, raw_token):
        session = self.snapshot['sessions'].get(token_hash(raw_token or ''))
        if not session or float(session['expires']) < time.time():
            raise Problem('Войдите в систему', 401)
        user = self.snapshot['users'].get(session['user_id'])
        if not user or user['blocked']:
            raise Problem('Учётная запись заблокирована', 401)
        return user, session

    @staticmethod
    def public_user(user):
        return {k: v for k, v in user.items() if k not in ('mask', 'password_hash')}

    def permitted(self, user):
        return [c for cid, c in self.snapshot['cameras'].items() if user['mask'] & (1 << cid)]

    def _session(self, conn, uid):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        conn.execute('DELETE FROM sessions WHERE expires_at<now()')
        conn.execute('INSERT INTO sessions(token_hash,user_id,csrf) VALUES (%s,%s,%s)', (token_hash(token), uid, csrf))
        return token

    def bootstrap(self, data):
        login, hashed = login_name(data.get('login')), password_hash(data.get('password'))
        first, last = text(data.get('first_name'), 'Имя'), text(data.get('last_name'), 'Фамилия')
        def action(conn):
            if conn.execute('SELECT EXISTS(SELECT 1 FROM users) AS exists').fetchone()['exists']:
                raise Problem('Первый администратор уже создан', 409)
            uid = conn.execute("INSERT INTO users(login,password_hash,first_name,last_name,role) VALUES (%s,%s,%s,%s,'admin') RETURNING id",
                               (login, hashed, first, last)).fetchone()['id']
            return self._session(conn, uid)
        return self.mutate(action)

    def login(self, data):
        login = login_name(data.get('login'))
        with self.pool.connection() as conn:
            user = conn.execute('SELECT id,password_hash,blocked FROM users WHERE login=%s', (login,)).fetchone()
        matches = password_matches(data.get('password'), user['password_hash'] if user else self.dummy_hash)
        if not matches or not user or user['blocked']:
            raise Problem('Неверный логин или пароль', 401)
        def action(conn):
            current = conn.execute('SELECT blocked FROM users WHERE id=%s', (user['id'],)).fetchone()
            if current['blocked']:
                raise Problem('Неверный логин или пароль', 401)
            return self._session(conn, user['id'])
        return self.mutate(action)

    def logout(self, token):
        self.mutate(lambda c: c.execute('DELETE FROM sessions WHERE token_hash=%s', (token_hash(token),)))

    def check_grants(self, data):
        if not set(data['camera_ids']) <= self.snapshot['cameras'].keys() or not set(data['group_ids']) <= self.snapshot['groups'].keys():
            raise Problem('Камера или группа больше не существует. Обновите страницу')

    def _admin(self, conn, uid):
        row = conn.execute('SELECT role,blocked FROM users WHERE id=%s', (uid,)).fetchone()
        if not row or row['role'] != 'admin' or row['blocked']:
            raise Problem('Нужны права администратора', 403)

    def invite(self, actor, data):
        p, token = profile(data), secrets.token_urlsafe(32)
        def action(conn):
            self._admin(conn, actor)
            self.check_grants(p)
            row = conn.execute('INSERT INTO invitations(token_hash,first_name,last_name,role,camera_ids,group_ids,creator_id) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                               (token_hash(token), p['first_name'], p['last_name'], p['role'], p['camera_ids'], p['group_ids'], actor)).fetchone()
            return dict(id=row['id'], token=token)
        return self.mutate(action)

    def _invitation(self, conn, token):
        if not isinstance(token, str) or not 30 <= len(token) <= 100:
            raise Problem('Приглашение недействительно или уже использовано', 410)
        row = conn.execute("SELECT i.* FROM invitations i JOIN users u ON u.id=i.creator_id WHERE token_hash=%s AND used_by IS NULL AND NOT revoked AND expires_at>now() AND u.role='admin' AND NOT u.blocked FOR UPDATE OF i",
                           (token_hash(token),)).fetchone()
        if not row:
            raise Problem('Приглашение недействительно или уже использовано', 410)
        return row

    def preview_invite(self, token):
        with self.pool.connection() as conn:
            row = self._invitation(conn, token)
            return {k: row[k] for k in ('first_name', 'last_name', 'role')}

    def register(self, data):
        login, hashed = login_name(data.get('login')), password_hash(data.get('password'))
        def action(conn):
            invite = self._invitation(conn, data.get('token'))
            uid = conn.execute('INSERT INTO users(login,password_hash,first_name,last_name,role,camera_ids,group_ids) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                               (login, hashed, invite['first_name'], invite['last_name'], invite['role'], invite['camera_ids'], invite['group_ids'])).fetchone()['id']
            conn.execute('UPDATE invitations SET used_by=%s WHERE id=%s', (uid, invite['id']))
            return self._session(conn, uid)
        return self.mutate(action)

    def invitations(self):
        with self.pool.connection() as conn:
            rows = conn.execute('SELECT id,first_name,last_name,role,used_by,revoked,created_at::text,expires_at::text FROM invitations ORDER BY id DESC LIMIT 200').fetchall()
        return rows

    def revoke_invite(self, actor, iid):
        def action(conn):
            self._admin(conn, actor)
            conn.execute('UPDATE invitations SET revoked=true WHERE id=%s', (iid,))
        self.mutate(action)

    def edit_user(self, actor, uid, data):
        p = profile(data)
        blocked = data.get('blocked')
        if type(blocked) is not bool:
            raise Problem('Неверный статус блокировки')
        def action(conn):
            self._admin(conn, actor)
            self.check_grants(p)
            row = conn.execute('SELECT role,blocked FROM users WHERE id=%s FOR UPDATE', (uid,)).fetchone()
            if not row:
                raise Problem('Пользователь не найден', 404)
            if row['role'] == 'admin' and not row['blocked'] and (p['role'] != 'admin' or blocked):
                count = conn.execute("SELECT count(*) AS n FROM users WHERE role='admin' AND NOT blocked").fetchone()['n']
                if count <= 1:
                    raise Problem('Нельзя заблокировать или понизить последнего администратора', 409)
            conn.execute('UPDATE users SET first_name=%s,last_name=%s,role=%s,blocked=%s,camera_ids=%s,group_ids=%s WHERE id=%s',
                         (p['first_name'], p['last_name'], p['role'], blocked, p['camera_ids'], p['group_ids'], uid))
            if blocked:
                conn.execute('DELETE FROM sessions WHERE user_id=%s', (uid,))
        self.mutate(action)

    def save_group(self, actor, gid, data):
        name, cameras = text(data.get('name'), 'Название группы'), ids(data.get('camera_ids', []))
        def action(conn):
            self._admin(conn, actor)
            self.check_grants(dict(camera_ids=cameras, group_ids=[]))
            if gid == 1:
                raise Problem('Группа «Все камеры» изменяется автоматически', 409)
            if gid:
                row = conn.execute('UPDATE camera_groups SET name=%s,camera_ids=%s WHERE id=%s AND NOT builtin RETURNING id', (name, cameras, gid)).fetchone()
                if not row:
                    raise Problem('Группа не найдена', 404)
            else:
                row = conn.execute('INSERT INTO camera_groups(name,camera_ids) VALUES (%s,%s) RETURNING id', (name, cameras)).fetchone()
            return row
        return self.mutate(action)

    def delete_group(self, actor, gid):
        def action(conn):
            self._admin(conn, actor)
            if gid == 1:
                raise Problem('Группу «Все камеры» нельзя удалить', 409)
            conn.execute('UPDATE users SET group_ids=array_remove(group_ids,%s) WHERE %s=ANY(group_ids)', (gid,gid))
            conn.execute('UPDATE invitations SET group_ids=array_remove(group_ids,%s) WHERE %s=ANY(group_ids)', (gid,gid))
            conn.execute('DELETE FROM camera_groups WHERE id=%s', (gid,))
        self.mutate(action)

    def sync_cameras(self, names):
        if not isinstance(names, list) or len(names) > 256 or any(not isinstance(n, str) for n in names):
            raise Problem('Регистратор вернул некорректный список камер')
        rows = [dict(id=i+1, source_name=n[:120], display_name=friendly_name(n[:120], i+1)) for i,n in enumerate(names)]
        def action(conn):
            conn.execute('UPDATE cameras SET active=false')
            conn.execute('INSERT INTO cameras(id,source_name,display_name) SELECT id,source_name,display_name FROM jsonb_to_recordset(%s) AS x(id integer,source_name text,display_name text) ON CONFLICT(id) DO UPDATE SET source_name=excluded.source_name,active=true', (Jsonb(rows),))
        self.mutate(action)

    def rename_cameras(self, actor, data):
        if not isinstance(data, list) or len(data) > 256:
            raise Problem('Неверный список камер')
        rows = [dict(id=ids([r.get('id')])[0], name=text(r.get('name'), 'Имя камеры', 120)) for r in data if isinstance(r, dict)]
        if len(rows) != len(data) or len({r['id'] for r in rows}) != len(rows):
            raise Problem('Неверный список камер')
        def action(conn):
            self._admin(conn, actor)
            self.check_grants(dict(camera_ids=[r['id'] for r in rows], group_ids=[]))
            conn.execute('UPDATE cameras c SET display_name=x.name FROM jsonb_to_recordset(%s) AS x(id integer,name text) WHERE c.id=x.id', (Jsonb(rows),))
        self.mutate(action)

    def layout(self, user):
        with self.pool.connection() as conn:
            row = conn.execute('SELECT value FROM layouts WHERE user_id=%s', (user['id'],)).fetchone()
        if not row:
            allowed = self.permitted(user)
            return dict(columns=1, tiles=[dict(id=secrets.token_hex(8), camera_id=allowed[0]['id'] if allowed else None, width=1, height=1)])
        value = row['value']
        for tile in value['tiles']:
            if tile['camera_id'] and not user['mask'] & (1 << tile['camera_id']):
                tile['camera_id'] = None
        return value

    def save_layout(self, user, value):
        if not isinstance(value, dict) or type(value.get('columns')) is not int or not 1 <= value['columns'] <= 12 or not isinstance(value.get('tiles'), list) or len(value['tiles']) > 64:
            raise Problem('Сетка: от 1 до 12 колонок и до 64 экранов')
        tiles, seen = [], set()
        for tile in value['tiles']:
            if not isinstance(tile, dict) or not isinstance(tile.get('id'), str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', tile['id']) or tile['id'] in seen:
                raise Problem('Неверный экран')
            seen.add(tile['id'])
            cid = tile.get('camera_id')
            if cid is not None and (type(cid) is not int or cid not in self.snapshot['cameras'] or not user['mask'] & (1 << cid)):
                raise Problem('Нет доступа к камере', 403)
            if type(tile.get('width')) is not int or not 1 <= tile['width'] <= 12 or type(tile.get('height')) is not int or not 1 <= tile['height'] <= 6:
                raise Problem('Неверный размер экрана')
            tiles.append(dict(id=tile['id'],camera_id=cid,width=tile['width'],height=tile['height']))
        result = dict(columns=value['columns'], tiles=tiles)
        def action(conn):
            # Recheck against the current snapshot after waiting for another writer.
            current = self.snapshot['users'].get(user['id'])
            if not current or current['blocked'] or any(t['camera_id'] and not current['mask'] & (1 << t['camera_id']) for t in tiles):
                raise Problem('Доступ изменился. Обновите страницу', 403)
            conn.execute('INSERT INTO layouts(user_id,value) VALUES (%s,%s) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value,updated_at=now()', (user['id'], Jsonb(result)))
        self.mutate(action)
        return result

    def connection(self):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='connection'").fetchone()
        return json.loads(self.cipher.decrypt(row['value'].encode())) if row and row['value'] else None

    def set_connection(self, actor, data):
        value = dict(SERIAL_NUM=text(data.get('ID'), 'ID',128), USERNAME=text(data.get('UserName'), 'UserName',128), PASSWORD=text(data.get('Password'), 'Password',256))
        encrypted = self.cipher.encrypt(json.dumps(value).encode()).decode()
        def action(conn):
            self._admin(conn, actor)
            conn.execute("INSERT INTO settings(key,value) VALUES ('connection',%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encrypted,))
        self.mutate(action)

    def import_legacy(self):
        def action(conn):
            if conn.execute("SELECT 1 FROM settings WHERE key='legacy_imported'").fetchone():
                return
            path = ROOT / '.env'
            values = {}
            if path.exists():
                for line in path.read_text().splitlines():
                    if '=' in line and not line.lstrip().startswith('#'):
                        key,val = line.split('=',1)
                        values[key.strip()] = val.strip().strip('\"\'')
            if all(values.get(k) for k in ('SERIAL_NUM','USERNAME','PASSWORD')):
                secret = {k: values[k] for k in ('SERIAL_NUM','USERNAME','PASSWORD')}
                conn.execute("INSERT INTO settings(key,value) VALUES ('connection',%s) ON CONFLICT DO NOTHING", (self.cipher.encrypt(json.dumps(secret).encode()).decode(),))
            conn.execute("INSERT INTO settings(key,value) VALUES ('legacy_imported','1')")
        self.mutate(action)
