import tempfile,unittest
from pathlib import Path
from spotify_poc.storage import SQLiteStore
from test_history_gap import fixture

class CheckpointTests(unittest.TestCase):
    def test_unchanged_queue_and_history_do_not_rewrite_rows_on_journal_checkpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SQLiteStore(Path(folder))
            try:
                _,state=fixture();store.write('state.json',state);changes=store.db.total_changes
                state['write_journal']={'sent':0,'uncertain':True};store.write('state.json',state)
                self.assertEqual(store.db.total_changes-changes,1)
                state['queue'][0]['name']='Updated display';store.write('state.json',state)
                self.assertEqual(store.read('state.json')['queue'][0]['name'],'Updated display')
            finally:store.close()

    def test_rollback_does_not_leave_stale_metadata_or_sequence_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SQLiteStore(Path(folder))
            try:
                _,state=fixture();store.write('state.json',state);original=store.read('state.json')
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        state['queue'][0]['name']='Changed';state['queue']=state['queue'][:1];store.write('state.json',state);raise RuntimeError('interrupted')
                self.assertEqual(store.read('state.json'),original)
                store.write('state.json',state);self.assertEqual(len(store.read('state.json')['queue']),1);self.assertEqual(store.read('state.json')['queue'][0]['name'],'Changed')
            finally:store.close()
