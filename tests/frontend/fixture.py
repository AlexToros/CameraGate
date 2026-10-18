import os
import tempfile
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from aiohttp import web
from backend.store import Store, password_hash
from backend.server import create_app
from tests.backend.test_api import FakeStreams

base = os.environ['TEST_DATABASE_URL']
runtime = tempfile.TemporaryDirectory(prefix='video-viewer-ui-')

schema = 'admin_visual_audit_' + str(os.getpid())
with psycopg.connect(base, autocommit=True) as connection:
    connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
store = Store(make_conninfo(base, options='-c search_path=' + schema), runtime.name)
store.sync_cameras(['Вход в подъезд', 'Двор — центральный вход', 'Парковка у второго подъезда', 'Детская площадка — вид со стороны двора'] + ['Камера ' + str(i) + ' — проход вдоль дома' for i in range(5, 33)])
store.bootstrap(dict(login='auditadmin', password='audit123', first_name='Александр', last_name='Тестовый'))
with store.pool.connection() as connection:
    connection.execute("INSERT INTO users(login,password_hash,first_name,last_name,role,blocked,camera_ids,group_ids) VALUES ('neighbour',%s,'Анна','Константинопольская','user',false,'{1,2,3}','{1}'),('blocked',%s,'Константин','Александровский','user',true,'{}','{}')", [password_hash('audit123'), password_hash('audit123')])
    connection.execute("INSERT INTO camera_groups(name,camera_ids) VALUES ('Входы в подъезды','{1,2}'),('Двор и детская площадка','{3,4,5}'),('Парковка со стороны второго подъезда','{6,7,8}'),('Камеры по периметру дома','{9,10,11,12}')")
    connection.execute("INSERT INTO invitations(token_hash,first_name,last_name,role,creator_id) VALUES ('audit-invite-one','Екатерина','Константинопольская','user',1),('audit-invite-two','Алексей','Тестовый','admin',1)")
store.refresh()
streams = FakeStreams()
streams.config = {'test': True}
try:
    web.run_app(create_app(store, streams, auto_sync=False), host='127.0.0.1', port=int(os.environ.get('UI_TEST_PORT', '8082')), access_log=None)
finally:
    store.close()
    runtime.cleanup()
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
