import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from spotify_app.client import SpotifyError
from spotify_ui.controller import Controller
from spotify_ui.server import Server
from fake_spotify import FakeSpotify

class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fake=FakeSpotify()
        self.controller=self.new_controller();self.addCleanup(self.controller.close)
    def new_controller(self):
        controller=Controller(self.tmp.name,client_factory=lambda *args,**kwargs:self.fake)
        controller.accounts.register('test-account',self.fake.token)
        return controller
    def wait(self,job):
        for _ in range(300):
            value=self.controller.job(job['id'])
            if value['status'] in ('failed','complete','cancelled'):return value
            time.sleep(.01)
        self.fail('Worker did not finish')
    def create(self):
        return self.wait(self.controller.submit('create',dict(account='test-account',name='My mix',sources=['SourceA','SourceB'],minimum=3,normal=8,rules=[dict(id='track',kind='track',window=10,strength='REQUIRED')])))
    def test_saved_sources_available_during_cooldown_without_http(self):
        from spotify_app.storage import SQLiteStore
        self.wait(self.controller.submit('sources',{'account':'test-account'}))
        with patch('spotify_app.client.http_json',side_effect=AssertionError('No network')),patch.object(self.controller,'cooldown_until',return_value=time.time()+3600):
            result=self.controller.source_status('test-account')
            self.assertTrue(result['cached'])
            self.assertEqual(len(result['sources']),3)
            self.assertGreater(result['cooldown_until'],time.time())
        self.create()
        store=SQLiteStore(Path(self.tmp.name)/'.ui',self.controller.database)
        try:
            store.cache_write('ui','sources-test-account',{'rows':[{'id':'target','name':'Generated'},{'id':'SourceA','name':'Source'}]})
            with patch.object(self.controller,'generated_targets',return_value={'test-account':{'target'}}):
                self.assertEqual([r['id'] for r in self.controller.source_status('test-account')['sources']],['SourceA'])
        finally:store.close()
        with self.assertRaises(SpotifyError):self.controller.source_status('unknown')

    def test_cached_sources_survive_restart_and_expired_cooldown(self):
        self.wait(self.controller.submit('sources',{'account':'test-account'}))
        restarted=self.new_controller()
        try:
            with patch('spotify_app.client.http_json',side_effect=AssertionError('No network')),patch.object(restarted,'cooldown_until',return_value=time.time()-1):
                result=restarted.source_status('test-account')
                self.assertTrue(result['cached'])
                self.assertEqual(len(result['sources']),3)
                self.assertIsNone(result['cooldown_until'])
        finally:restarted.close()

    def test_fake_spotify_service_loop_never_uses_http(self):
        with patch('spotify_app.client.http_json',side_effect=AssertionError('Fake transport should not use HTTP')):
            sources=self.wait(self.controller.submit('sources',{'account':'test-account'}))
            self.assertEqual(len(sources['data']['sources']),3)
            created=self.create();self.assertEqual(created['status'],'complete',created)
            mix=created['data']['mix'];snapshot=self.controller.snapshot()['mixes'][0]
            self.assertEqual(snapshot['queue_size'],8);self.assertEqual(snapshot['status'],'ready')
            preview=self.wait(self.controller.submit('preview',dict(mix=mix)))
            self.assertEqual(preview['data']['summary']['consumed'],0)
            self.assertFalse(preview['data']['summary']['write_needed'])
            applied=self.wait(self.controller.submit('apply',dict(mix=mix,preview_id=preview['data']['preview_id'])))
            self.assertEqual(applied['status'],'complete')
            self.assertNotIn('preview',json.dumps(self.controller.snapshot()['mixes']))
    def test_source_selector_excludes_registered_generated_targets(self):
        from spotify_app.storage import SQLiteStore
        store=SQLiteStore(Path(self.tmp.name)/'saved',self.controller.database)
        try:
            store.write('setup.json',dict(account='test-account',target='SourceA'))
            store.write('state.json',dict(account='test-account',target='SourceB',history=[],queue=[]))
        finally:store.close()
        result=self.wait(self.controller.submit('sources',{'account':'test-account'}))
        self.assertEqual(result['status'],'complete',result)
        self.assertEqual([r['id'] for r in result['data']['sources']],['SourceC'])

    def test_removed_generated_target_stays_excluded(self):
        from spotify_app.storage import SQLiteStore
        root=Path(self.tmp.name)/'saved'
        store=SQLiteStore(root,self.controller.database)
        store.write('state.json',dict(account='test-account',target='SourceA',history=[],queue=[]))
        store.close()
        removed=self.wait(self.controller.submit('delete',dict(mix=self.controller.identity(root))))
        self.assertEqual(removed['status'],'complete',removed)
        result=self.wait(self.controller.submit('sources',dict(account='test-account')))
        self.assertEqual([r['id'] for r in result['data']['sources']],['SourceB','SourceC'])
        # Provenance survives backup pruning and remains account scoped.
        import shutil
        shutil.rmtree(Path(self.tmp.name)/'.sync-backups')
        result=self.wait(self.controller.submit('sources',dict(account='test-account')))
        self.assertEqual([r['id'] for r in result['data']['sources']],['SourceB','SourceC'])

    def test_invalid_create_inputs_do_not_make_workspace(self):
        cases=[dict(name='',sources=['SourceA']),dict(name='x',sources=['../../bad']),dict(name='x',sources=['SourceA'],minimum=30,normal=20),dict(name='x',sources=['SourceA'],rules=[dict(id='genre',kind='genre',window=1,strength='REQUIRED')])]
        for data in cases:
            with self.subTest(data=data),self.assertRaises((SpotifyError,ValueError)):self.controller.submit('create',data)
        self.assertFalse((Path(self.tmp.name)/'mixes').exists())
    def test_expired_preview_is_not_applied(self):
        created=self.create();mix=created['data']['mix']
        preview=self.wait(self.controller.submit('preview',{'mix':mix}))
        identity=preview['data']['preview_id'];self.controller.previews[identity]['expires']=0
        result=self.wait(self.controller.submit('apply',dict(mix=mix,preview_id=identity)))
        self.assertEqual(result['status'],'failed');self.assertIn('expired',result['error'])
        self.assertEqual(self.controller.snapshot()['mixes'][0]['status'],'ready')
    def test_workspace_ids_cannot_escape_project(self):
        with self.assertRaises(SpotifyError):self.controller.workspace('../../outside')
    def test_active_job_can_be_cancelled_without_blocking_requests(self):
        entered=threading.Event();released=threading.Event()
        original=self.fake.pages
        def slow(path):
            entered.set();released.wait(2);yield from original(path)
        self.fake.pages=slow
        job=self.controller.submit('sources',{'account':'test-account'});self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(SpotifyError,'already running'):self.controller.submit('sources',{'account':'test-account'})
        self.assertEqual(self.controller.cancel(job['id'])['status'],'cancelling')
        released.set();self.assertEqual(self.wait(job)['status'],'cancelled')
        self.assertIsNone(self.controller.snapshot()['active'])
    def test_http_requires_host_origin_and_mutation_token(self):
        server=Server(('127.0.0.1',0),self.controller)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path,data=None,headers=None):
            raw=json.dumps(data).encode() if data is not None else None
            req=Request(server.origin+path,data=raw,headers={'Content-Type':'application/json',**(headers or {})})
            with urlopen(req,timeout=2) as response:return response.status,json.loads(response.read())
        status,bootstrap=request('/api/bootstrap');self.assertEqual(status,200)
        self.assertNotIn('token',request('/api/state')[1])
        for headers in [{},{'Origin':server.origin},{'Origin':'https://outside.example','X-App-Token':bootstrap['token']}]:
            with self.subTest(headers=headers),self.assertRaises(HTTPError) as error:request('/api/actions/sources',{'account':'test-account'},headers)
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:request('/api/state',headers={'Host':'outside.example'})
        self.assertEqual(error.exception.code,403)
        status,job=request('/api/actions/sources',{'account':'test-account'},{'Origin':server.origin,'X-App-Token':bootstrap['token']})
        self.assertEqual(status,202);self.assertEqual(self.wait(job)['status'],'complete')

    def test_login_callback_can_be_cancelled_without_token_exchange(self):
        from spotify_app.client import login
        cancelled=threading.Event();cancelled.set();urls=[]
        with patch('spotify_app.client.HTTPServer') as server,patch('spotify_app.client.http_json') as transport:
            with self.assertRaisesRegex(SpotifyError,'cancelled'):
                login('a'*32,Path(self.tmp.name)/'token.json',on_authorize=urls.append,cancellation=cancelled)
        self.assertEqual(len(urls),1)
        self.assertIn('code_challenge_method=S256',urls[0]);transport.assert_not_called()
        self.assertFalse((Path(self.tmp.name)/'token.json').exists())

    def test_long_saved_cooldown_returns_promptly_without_waiting(self):
        job={'progress':None}
        with self.assertRaisesRegex(SpotifyError,'temporarily limiting requests'):
            self.controller.emit(job,dict(stage='cooldown',remaining_seconds=47826))
        self.assertIsNone(job['progress'])
        self.controller.emit(job,dict(stage='cooldown',remaining_seconds=5))
        self.assertEqual(job['progress']['remaining_seconds'],5)

    def test_remove_setup_is_local_and_preserves_other_mix(self):
        first=self.create();second=self.create()
        mix=first['data']['mix'];other=second['data']['mix']
        with patch.object(self.controller,'connect',side_effect=AssertionError('Removal must not contact Spotify')):
            removed=self.wait(self.controller.submit('delete',{'mix':mix}))
        self.assertEqual(removed['status'],'complete',removed)
        self.assertEqual([m['id'] for m in self.controller.snapshot()['mixes']],[other])
        self.assertTrue(list((Path(self.tmp.name)/'.sync-backups').glob('*/state.json')))
