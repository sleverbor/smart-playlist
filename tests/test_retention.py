import tempfile,unittest,json
from pathlib import Path
from spotify_app.storage import SQLiteStore
from spotify_app.retention import maintain,DAY
from test_history_gap import fixture

class RetentionTests(unittest.TestCase):
    def test_prunes_old_logs_and_indexes_but_preserves_queue_and_pending_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SQLiteStore(tmp);_,state=fixture();state['status']='pending';store.write('state.json',state)
            before=store.read('state.json');store.write('history-backup-1.json',state)
            store.db.execute("INSERT INTO tracks VALUES(?,?,?,?,?)".replace('?,?,?,?,?','?,?,?,?'),(store.workspace,'stale','album','{}'))
            store.event('old',{'stage':'request'})
            store.db.execute('UPDATE events SET created=0');store.db.commit()
            maintain(store,now=40*DAY)
            self.assertEqual(store.read('state.json'),before)
            self.assertIsNotNone(store.read('history-backup-1.json'))
            self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM tracks WHERE track_id='stale'").fetchone()[0],0)
            store.close()

    def test_bounds_history_backups_and_preserves_active_source_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SQLiteStore(tmp);_,state=fixture();store.write('state.json',state)
            for i in range(8):store.write(f'history-backup-{i}.json',state)
            store.write('setup.json',dict(imports={'source':dict(snapshot='1',tracks=[])}))
            store.cache_write('test','active',{'playlist':'source'});store.cache_write('test','stale',{'playlist':'gone'})
            store.db.execute('UPDATE cache SET updated=0');store.db.commit()
            maintain(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM documents WHERE name LIKE 'history-backup-%'").fetchone()[0],3)
            self.assertIsNotNone(store.cache_read('test','active'));self.assertIsNone(store.cache_read('test','stale'))
            store.close()

    def test_log_cap_and_removed_setup_backup_limit(self):
        import time,os
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp);store=SQLiteStore(project/'mix',project/'.spotify-accounts/application.sqlite')
            now=time.time()
            store.db.executemany('INSERT INTO events(workspace,operation,created,payload) VALUES(?,?,?,?)',[(store.workspace,'test',now,'{}')]*5010);store.db.commit()
            for i in range(12):
                p=project/'.sync-backups'/str(i);p.mkdir(parents=True);(p/'state.json').write_text('{}');os.utime(p,(now-i,now-i))
            maintain(store,now=now)
            self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0],5000)
            self.assertEqual(len(list((project/'.sync-backups').iterdir())),10)
            store.close()
