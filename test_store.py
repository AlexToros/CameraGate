"""Integration tests against a disposable schema in real PostgreSQL.

TEST_DATABASE_URL must point to a local database whose role can create schemas.
No production tables or identities are touched.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import tempfile
import unittest

import psycopg
from psycopg.conninfo import make_conninfo
from store import Store, Problem, password_matches

PASSWORD = 'test-password-long-123'


class DatabaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = os.environ.get('TEST_DATABASE_URL')
        if not base:
            raise unittest.SkipTest('Set TEST_DATABASE_URL to run PostgreSQL integration tests')
        cls.schema = 'test_'+secrets.token_hex(8)
        cls.base = base
        with psycopg.connect(base,autocommit=True) as conn:
            conn.execute('CREATE SCHEMA '+cls.schema)
        cls.dsn = make_conninfo(base, options='-c search_path='+cls.schema)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base,autocommit=True) as conn:
            conn.execute('DROP SCHEMA '+cls.schema+' CASCADE')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.dsn, self.temp.name)
        with self.store.pool.connection() as conn:
            conn.execute('TRUNCATE sessions,layouts,invitations,users,cameras,camera_groups,settings RESTART IDENTITY CASCADE')
            conn.execute("INSERT INTO camera_groups(id,name,builtin) VALUES (1,'Все камеры',true)")
            conn.execute("SELECT setval(pg_get_serial_sequence('camera_groups','id'),1)")
        self.store.refresh()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def admin(self):
        token = self.store.bootstrap(dict(login='admin',password=PASSWORD,first_name='Первый',last_name='Админ'))
        return self.store.user(token)[0],token

    def invited(self, admin, **kwargs):
        data = dict(first_name='Иван',last_name='Иванов',role='user',camera_ids=[1],group_ids=[])
        data.update(kwargs)
        invite = self.store.invite(admin['id'],data)
        token = self.store.register(dict(token=invite['token'],login='user',password=PASSWORD))
        return self.store.user(token)[0],token


class StoreTests(DatabaseCase):
    def test_bootstrap_race_and_password_storage(self):
        def create(i):
            try:
                return self.store.bootstrap(dict(login='admin'+str(i),password=PASSWORD,first_name='A',last_name='B'))
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(create, [1,2]))
        self.assertEqual(sum(isinstance(r,str) for r in results),1)
        self.assertIn(409,results)
        with self.store.pool.connection() as conn:
            hashed = conn.execute('SELECT password_hash FROM users').fetchone()['password_hash']
        self.assertNotIn(PASSWORD,hashed)
        self.assertTrue(password_matches(PASSWORD,hashed))

    def test_invite_is_atomic_single_use_and_identity_is_fixed(self):
        admin,_ = self.admin()
        invite = self.store.invite(admin['id'],dict(first_name='Мария',last_name='Петрова',role='user',group_ids=[1]))
        def consume(i):
            try:
                return self.store.register(dict(token=invite['token'],login='user'+str(i),password=PASSWORD,role='admin',first_name='Подмена'))
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(consume,[1,2]))
        self.assertEqual(sum(isinstance(r,str) for r in results),1)
        self.assertIn(410,results)
        user = next(u for u in self.store.snapshot['users'].values() if u['role']=='user')
        self.assertEqual(user['first_name'],'Мария')
        self.assertEqual(user['last_name'],'Петрова')

    def test_group_union_revocation_block_and_last_admin(self):
        admin,_ = self.admin()
        self.store.sync_cameras(['Один','Два','Три'])
        gid = self.store.save_group(admin['id'],None,dict(name='Двор',camera_ids=[2,3]))['id']
        user,token = self.invited(admin,group_ids=[gid])
        self.assertEqual([c['id'] for c in self.store.permitted(user)],[1,2,3])
        self.store.save_group(admin['id'],gid,dict(name='Двор',camera_ids=[2]))
        self.assertEqual([c['id'] for c in self.store.permitted(self.store.user(token)[0])],[1,2])
        data = self.store.public_user(self.store.user(token)[0])
        self.store.edit_user(admin['id'],user['id'],dict(data,blocked=True))
        with self.assertRaises(Problem):
            self.store.user(token)
        with self.assertRaises(Problem):
            self.store.edit_user(admin['id'],admin['id'],dict(self.store.public_user(admin),blocked=True))
        with self.assertRaises(Problem):
            self.store.delete_group(admin['id'],1)

    def test_layout_survives_store_restart_and_filters_lost_access(self):
        admin,_ = self.admin()
        self.store.sync_cameras(['Один','Два'])
        user,token = self.invited(admin)
        layout = dict(columns=3,tiles=[dict(id='a',camera_id=1,width=2,height=2),dict(id='b',camera_id=1,width=1,height=1)])
        self.store.save_layout(user,layout)
        self.store.close()
        self.store = Store(self.dsn,self.temp.name)
        user = self.store.user(token)[0]
        self.assertEqual(self.store.layout(user),layout)
        with self.assertRaises(Problem):
            self.store.save_layout(user,dict(columns=1,tiles=[dict(id='bad',camera_id=2,width=1,height=1)]))
        self.store.edit_user(admin['id'],user['id'],dict(self.store.public_user(user),camera_ids=[],blocked=False))
        self.assertTrue(all(t['camera_id'] is None for t in self.store.layout(self.store.user(token)[0])['tiles']))

    def test_encrypted_connection_and_custom_names_survive_sync(self):
        admin,_ = self.admin()
        data = dict(ID='serial-not-secret-test',UserName='upstream-user',Password='upstream-password')
        self.store.set_connection(admin['id'],data)
        with self.store.pool.connection() as conn:
            encrypted = conn.execute("SELECT value FROM settings WHERE key='connection'").fetchone()['value']
        self.assertNotIn(data['Password'],encrypted)
        self.assertEqual(self.store.connection()['PASSWORD'],data['Password'])
        self.store.sync_cameras(['One','Two'])
        self.store.rename_cameras(admin['id'],[dict(id=1,name='Вход')])
        self.store.sync_cameras(['Changed','Two','New'])
        self.assertEqual(self.store.snapshot['cameras'][1]['name'],'Вход')
        self.assertEqual(len(self.store.permitted(self.store.snapshot['users'][admin['id']])),3)

    def test_revoked_creator_and_login_conflict_do_not_consume_invite(self):
        admin,_ = self.admin()
        invite = self.store.invite(admin['id'],dict(first_name='A',last_name='B',role='admin'))
        with self.assertRaises(Problem):
            self.store.register(dict(token=invite['token'],login='admin',password=PASSWORD))
        self.assertEqual(self.store.preview_invite(invite['token'])['role'],'admin')
        self.store.revoke_invite(admin['id'],invite['id'])
        with self.assertRaises(Problem):
            self.store.preview_invite(invite['token'])


if __name__ == '__main__':
    unittest.main()
