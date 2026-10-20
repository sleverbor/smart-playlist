import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from spotify_app.client import Client,save_json
from spotify_app.storage import SQLiteStore

class AccountCacheTests(unittest.TestCase):
    def test_relogin_reuses_source_but_other_accounts_and_apps_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=SQLiteStore(root/'mix',root/'db.sqlite')
            try:
                def client(refresh,app='app'):
                    path=root/'token.json';save_json(path,dict(client_id=app,refresh_token=refresh,access_token='unused',expires_at=9999999999))
                    return Client(path,store=store)
                first=client('old');old=first.token.copy()
                first.cache_write(first.cache/'source-s.json',dict(playlist='s',snapshot='v1',rows=[{'id':'track'}]))
                newer=client('new');newer.bind_account_cache('me',source_snapshots={'s':'v1'})
                newer.request=Mock(return_value={'snapshot_id':'v1'});newer.playlist=Mock(side_effect=AssertionError('must reuse source'))
                self.assertEqual(newer.source_playlist('s')[1],[{'id':'track'}])
                again=client('rotated');again.bind_account_cache('me')
                self.assertEqual(again.cache_scope,newer.cache_scope)
                self.assertIsNotNone(again.cache_read(again.cache/'source-s.json'))
                other=client('other');other.bind_account_cache('other-account')
                self.assertIsNone(other.cache_read(other.cache/'source-s.json'))
                app=client('new-app','other-app');app.bind_account_cache('me',old)
                self.assertIsNone(app.cache_read(app.cache/'source-s.json'))
            finally:store.close()
