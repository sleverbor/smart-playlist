import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock,patch

from spotify_app.storage import SQLiteStore
from spotify_app.accounts import Accounts
from spotify_app.progress import Progress, Cancelled
from spotify_app.service import Application
from spotify_app.writes import PreconditionFailed
from spotify_app.client import save_json,read_json,SpotifyError
from test_progressive import FakeSpotify
from test_history_gap import fixture,event


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def store(self,root=None,database=None):
        store=SQLiteStore(root or self.root,database)
        self.addCleanup(store.close);return store

    def test_migration_preserves_legacy_and_separates_history_future(self):
        pool,state=fixture();save_json(self.root/'state.json',state)
        old=(self.root/'state.json').read_bytes();store=self.store()
        self.assertEqual(store.read('state.json'),json.loads(old))
        updated=store.read('state.json');updated['generation']=2;store.write('state.json',updated)
        self.assertEqual((self.root/'state.json').read_bytes(),old)
        self.assertEqual(store.db.execute('SELECT kind,count(*) FROM sequences GROUP BY kind').fetchall(),[('history',1),('queue',3)])
        self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0],1)
        store.export(self.root/'export')
        self.assertEqual(read_json(self.root/'export/state.json'),updated)

    def test_import_checkpoints_append_rows_and_reset_changed_snapshot(self):
        store=self.store();raw=dict(id='a',artists=[{'id':'artist'}],album={'id':'album'})
        job=dict(imports={'s':dict(snapshot='1',tracks=[raw])})
        store.write('setup.json',job)
        changes=store.db.total_changes;store.write('setup.json',job)
        self.assertEqual(store.db.total_changes-changes,2) # source metadata + document, no track rows rewritten
        job['imports']['s']['tracks'].append(dict(raw,id='b'));store.write('setup.json',job)
        self.assertEqual(len(store.read('setup.json')['imports']['s']['tracks']),2)
        job['imports']['s']=dict(snapshot='2',tracks=[dict(raw,id='c')]);store.write('setup.json',job)
        self.assertEqual(store.db.execute('SELECT track_id FROM import_items').fetchall(),[('c',)])

    def test_transaction_rolls_back_queue_and_document_together(self):
        store=self.store();_,state=fixture();store.write('state.json',state);before=store.read('state.json')
        with self.assertRaises(RuntimeError):
            with store.transaction():
                state['queue']=[];store.write('state.json',state)
                store.write('decision-log.json',{'new':'report'})
                raise RuntimeError('interrupted')
        self.assertEqual(store.read('state.json'),before)
        self.assertIsNone(store.read('decision-log.json'))

    def test_workspaces_and_caches_do_not_leak(self):
        database=self.root/'common.sqlite';a=self.store(self.root/'a',database);b=self.store(self.root/'b',database)
        a.write('note.json',{'a':1});self.assertIsNone(b.read('note.json'))
        a.cache_write('account-a','page',{'playlist':'s','body':1})
        self.assertIsNone(b.cache_read('account-b','page'))
        self.assertEqual(b.cache_read('account-a','page')['body'],1)
        b.cache_delete('account-a',playlist='s');self.assertIsNone(a.cache_read('account-a','page'))

    def test_shared_login_reused_without_copying_tokens_to_new_workspace(self):
        accounts=Accounts(self.root/'accounts');token=dict(client_id='app',access_token='secret',refresh_token='refresh')
        path=accounts.register('me',token)
        self.assertEqual(accounts.select(self.root/'new'),path)
        accounts.register('other',dict(token,refresh_token='other'))
        with self.assertRaises(SpotifyError):accounts.select(self.root/'ambiguous')
        self.assertEqual(accounts.select(self.root/'other','other').parent,accounts.root)

    def test_cancellation_and_structured_progress_persist(self):
        store=self.store();events=[];cancel=Event();progress=Progress(store,events.append,cancel)
        progress.emit('generating',completed=1,total=20)
        self.assertEqual(events[0]['completed'],1)
        cancel.set()
        with self.assertRaises(Cancelled):progress.emit('publishing')
        self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_service_progressive_create_resume_sqlite(self):
        store=self.store();client=FakeSpotify(sources=2,pages=2)
        events=[];app=Application(client,self.root,store,Progress(store,events.append))
        app.connect();app.create(sources=['0','1'],rows=[],minimum=2,normal=6,name='Application test')
        state=store.read('state.json');self.assertEqual(state['status'],'ready');self.assertEqual(len(state['queue']),6)
        self.assertFalse((self.root/'state.json').exists())
        self.assertEqual(len(client.creates),1)
        self.assertTrue(any(e['stage']=='generating' for e in events))
        app.resume();self.assertEqual(len(client.creates),1)

    def preview_fixture(self):
        store=self.store();pool,state=fixture();store.write('state.json',state)
        client=Mock();client.request.return_value={'id':'me'}
        metadata={'owner':{'id':'me'},'snapshot_id':'v1'}
        rows=[{'item':{'id':t['id']}} for t in state['queue']]
        client.playlist.return_value=(metadata,rows);client.recent.return_value=[event(0)]
        loader=Mock(return_value=(pool,{'source':'v1'},0))
        app=Application(client,self.root,store,library_loader=loader);app.connect()
        return store,client,app,state,metadata,rows

    def test_preview_accepts_continued_playback_without_mutating_queue(self):
        store,client,app,state,metadata,rows=self.preview_fixture()
        preview=app.preview();client.replace.assert_not_called()
        client.recent.return_value=[event(0),event(1)]
        app.apply_preview(preview)
        self.assertEqual(store.read('state.json')['queue'],json.loads(json.dumps(state['queue'])))
        client.replace.assert_not_called()

    def test_late_target_edit_restores_baseline_without_remote_write(self):
        store,client,app,state,metadata,rows=self.preview_fixture();preview=app.preview()
        preview.state['queue']=preview.state['queue'][:1]
        client.playlist.side_effect=[(metadata,rows),({'owner':{'id':'me'},'snapshot_id':'changed'},rows)]
        with self.assertRaisesRegex(PreconditionFailed,'Target changed'):app.apply_preview(preview)
        self.assertEqual(store.read('state.json')['status'],'ready')
        client.replace.assert_not_called()

    def test_successful_apply_and_durable_pending_failure(self):
        store,client,app,state,metadata,rows=self.preview_fixture();preview=app.preview()
        preview.state['queue']=preview.state['queue'][:1]
        client.replace.side_effect=SpotifyError('uncertain write')
        with self.assertRaisesRegex(SpotifyError,'uncertain'):app.apply_preview(preview)
        self.assertEqual(store.read('state.json')['status'],'pending')
        self.assertIsNotNone(store.read('state.json')['write_guard'])
        self.assertEqual(client.replace.call_count,1)

    def test_recovery_refuses_divergent_remote_edit(self):
        store,client,app,state,metadata,rows=self.preview_fixture()
        state.update(status='pending',write_guard={'snapshot':'v1','actual':[t['id'] for t in state['queue']],
            'events':[]});store.write('state.json',state)
        client.playlist.return_value=(metadata,[{'item':{'id':'user-added'}}])
        with self.assertRaisesRegex(PreconditionFailed,'diverged'):app.recover_write()
        self.assertEqual(store.read('state.json')['status'],'pending');client.replace.assert_not_called()

    def test_application_rejects_concurrent_workspace_operation(self):
        import fcntl
        store,client,app,state,metadata,rows=self.preview_fixture()
        with (self.root/'lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(SpotifyError,'Another operation'):
                app.preview()

    def test_interrupted_setup_can_resume_without_creating_twice(self):
        store=self.store();client=FakeSpotify(sources=2,pages=2)
        def stop(event):
            if 'Ready to play in Spotify' in event.get('message',''):
                raise KeyboardInterrupt()
        app=Application(client,self.root,store,Progress(store,stop));app.connect()
        with self.assertRaises(KeyboardInterrupt):
            app.create(sources=['0','1'],rows=[],minimum=2,normal=6)
        self.assertEqual(store.read('state.json')['status'],'ready')
        app.progress=Progress(store)
        app.resume();self.assertEqual(len(client.creates),1)
        self.assertEqual(len(store.read('state.json')['queue']),6)

    def test_existing_bound_workspace_can_import_local_login(self):
        token=dict(client_id='app',access_token='local',refresh_token='refresh')
        save_json(self.root/'token.json',token)
        accounts=Accounts(self.root/'accounts')
        self.assertEqual(accounts.select(self.root,'existing-account'),self.root/'token.json')
        shared=accounts.register('existing-account',token)
        self.assertEqual(accounts.select(self.root,'existing-account'),shared)

    def test_pending_recovery_finishes_saved_plan_without_reading_new_playback(self):
        from spotify_app.sync import key
        store,client,app,state,metadata,rows=self.preview_fixture()
        state.update(status='pending',write_guard=dict(snapshot='v1',actual=[],events=[list(key(event(0)))]))
        store.write('state.json',state)
        client.playlist.return_value=(metadata,[])
        client.recent.return_value=[event(0),event(1)]
        def publish(c,p,s,**kw):
            s['status']='ready';s.pop('write_guard',None);kw['writer'](p,s)
        app.publisher=publish
        updated=app.recover_write()
        self.assertEqual(updated['status'],'ready')
        self.assertIsNone(store.read('decision-log.json'))
        self.assertEqual(store.read('state.json')['last_event'][0],event(0)['played_at'])
        client.recent.assert_not_called()
        client.request.assert_called_once_with('GET','/me')

    def test_shared_cooldown_import_preserves_longest_deadline(self):
        accounts=Accounts(self.root/'accounts');token=dict(client_id='app',access_token='t')
        save_json(self.root/'rate.json',dict(next=100))
        shared=accounts.import_legacy_rate(self.root,token)
        self.assertEqual(read_json(shared)['next'],100)
        save_json(shared,dict(next=200));accounts.import_legacy_rate(self.root,token)
        self.assertEqual(read_json(shared)['next'],200)

    def test_sqlite_http_cache_and_persisted_429_details(self):
        from spotify_app.client import Client,RateLimited
        from test_rate_limits import Clock
        store=self.store();clock=Clock();events=[]
        token=self.root/'token.json'
        save_json(token,dict(client_id='app',access_token='secret',refresh_token='refresh',expires_at=9999999999))
        client=Client(token,store=store,progress=Progress(store,events.append),clock=clock.time,sleep=clock.sleep)
        headers={'ETag':'v1','Cache-Control':'max-age=0'}
        with patch('spotify_app.client.http_json',side_effect=[({'items':[1]},headers,200),(None,headers,304)]) as transport:
            self.assertEqual(client.request('GET','/playlists/s/items'),{'items':[1]})
            self.assertEqual(client.request('GET','/playlists/s/items'),{'items':[1]})
            self.assertEqual(transport.call_args.kwargs['extra_headers']['If-None-Match'],'v1')
        with patch('spotify_app.client.http_json',side_effect=[RateLimited('4'),({}, {},200)]):
            client.request('GET','/me')
        saved=[json.loads(row[0]) for row in store.db.execute('SELECT payload FROM events')]
        rate=next(e for e in saved if e['stage']=='rate_limited')
        self.assertEqual(rate['retry_after'],'4');self.assertEqual(rate['retry_seconds'],4)
        self.assertEqual(client.metrics['cache_hits'],1)
        self.assertFalse(any('secret' in json.dumps(e) for e in saved))

    def test_cancelled_cooldown_does_not_issue_request(self):
        from spotify_app.client import Client
        from test_rate_limits import Clock
        clock=Clock();cancel=Event()
        token=self.root/'token.json'
        save_json(token,dict(client_id='app',access_token='secret',expires_at=9999999999))
        progress=Progress(cancellation=cancel)
        client=Client(token,progress=progress,clock=clock.time,sleep=clock.sleep)
        client.gate.defer(300);cancel.set()
        with patch('spotify_app.client.http_json') as transport:
            with self.assertRaises(Cancelled):client.request('GET','/me')
        transport.assert_not_called()
        self.assertEqual(read_json(self.root/'rate.json')['next'],1300)

    def test_ineligible_source_stops_before_setup_or_target_creation(self):
        store=self.store();client=Mock();client.request.side_effect=[{'id':'me'},{'owner':{'id':'other'},'collaborative':False}]
        app=Application(client,self.root,store);app.connect()
        with self.assertRaisesRegex(SpotifyError,'owned or accessible'):app.create(sources=['s'],rows=[])
        self.assertIsNone(store.read('setup.json'))
        self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))

    def test_legacy_mode_refuses_migrated_workspace(self):
        from spotify_app.__main__ import main
        accounts=self.root/'accounts';store=self.store(database=accounts/'application.sqlite')
        with patch('sys.argv',['p','--directory',str(self.root),'--accounts-directory',str(accounts),'--storage','json','sync']):
            with self.assertRaisesRegex(SpotifyError,'legacy JSON is stale'):main()

    def test_service_failure_is_persisted_for_ui_diagnostics(self):
        store,client,app,state,metadata,rows=self.preview_fixture()
        client.playlist.side_effect=SpotifyError('access revoked')
        with self.assertRaisesRegex(SpotifyError,'access revoked'):app.preview()
        self.assertEqual(store.latest_event()['stage'],'failed')
        self.assertEqual(store.latest_event()['error_type'],'SpotifyError')

    def test_explicit_history_recovery_can_resolve_recognized_pending_gap(self):
        store,client,app,state,metadata,rows=self.preview_fixture()
        state.update(status='pending',write_guard=dict(snapshot='v1',actual=[],events=[]))
        store.write('state.json',state);client.playlist.return_value=(metadata,[])
        client.recent.return_value=[event(1)]
        with self.assertRaisesRegex(SpotifyError,'Incomplete write'):app.preview()
        preview=app.preview(recovery=True)
        self.assertTrue(preview.report['pending_write_recovered'])
        self.assertFalse(preview.report['history_recovery']['consumption_known'])
        self.assertEqual(store.read('state.json')['status'],'pending')
        client.playlist.return_value=(metadata,[{'item':{'id':'unrecognized'}}])
        with self.assertRaisesRegex(PreconditionFailed,'diverged'):app.preview(recovery=True)

    def test_full_buffer_defers_remote_reconciliation_until_import_finishes(self):
        client=FakeSpotify(sources=2,pages=5);store=self.store();events=[]
        app=Application(client,self.root,store,Progress(store,events.append));app.connect()
        app.create(sources=['0','1'],rows=[],minimum=2,normal=2)
        self.assertTrue(any('buffer is full' in e.get('message','') for e in events))
        self.assertEqual(client.target_snapshot,1) # initial fill; final no-op is readback only
        self.assertEqual(store.read('setup.json')['status'],'complete')
        self.assertEqual(sum('Verified playlist:' in e.get('message','') for e in events),1)

    def test_playback_during_expansion_finishes_import_and_defers_write(self):
        from spotify_app.writes import apply, PlaybackChanged
        store=self.store();client=FakeSpotify(sources=2,pages=4)
        calls=[]
        def publish(*args,**kwargs):
            calls.append(1)
            if 2 <= len(calls) <= 4:
                raise PlaybackChanged('Playback changed before write')
            return apply(*args,**kwargs)
        app=Application(client,self.root,store,publisher=publish);app.connect()
        app.create(sources=['0','1'],rows=[],minimum=2,normal=6,name='Listening')
        state=store.read('state.json');job=store.read('setup.json')
        self.assertEqual(state['status'],'ready')
        self.assertEqual(len(state['queue']),2)
        self.assertEqual(client.target,[t['id'] for t in state['queue']])
        self.assertEqual(job['status'],'awaiting_pause')
        self.assertTrue(all(not entry['next'] for entry in job['imports'].values()))
        app.resume()
        self.assertEqual(store.read('setup.json')['status'],'complete')
        self.assertEqual(len(store.read('state.json')['queue']),6)
        self.assertEqual(len(client.creates),1)

    def test_rename_retries_verify_before_repeating_write(self):
        store=self.store();store.write('state.json',dict(status='ready',account='me',target='target'))
        store.write('setup.json',dict(name='New name',name_sync_pending=True))
        client=Mock();app=Application(client,self.root,store);app.account='me'
        client.request.side_effect=[dict(owner={'id':'me'},name='Old name'),SpotifyError('Interrupted')]
        with self.assertRaises(SpotifyError):app.rename_pending()
        self.assertTrue(store.read('setup.json')['name_sync_pending'])
        client.request.reset_mock();client.request.side_effect=None
        client.request.return_value=dict(owner={'id':'me'},name='New name')
        app.rename_pending()
        client.request.assert_called_once_with('GET','/playlists/target')
        self.assertFalse(store.read('setup.json')['name_sync_pending'])

    def test_rename_refuses_other_owner(self):
        store=self.store();store.write('state.json',dict(status='ready',account='me',target='target'))
        store.write('setup.json',dict(name='New name',name_sync_pending=True))
        client=Mock();client.request.return_value=dict(owner={'id':'other'},name='Old name')
        app=Application(client,self.root,store);app.account='me'
        with self.assertRaises(SpotifyError):app.rename_pending()
        client.request.assert_called_once_with('GET','/playlists/target')

    def test_recreate_ambiguous_creation_stops_duplicate_retry(self):
        store=self.store();store.write('state.json',dict(status='ready',account='me',target='gone',queue=[]))
        store.write('setup.json',dict(name='Saved name'))
        client=Mock();client.request.side_effect=[SpotifyError('Spotify HTTP 404',status=404),SpotifyError('Connection interrupted')]
        app=Application(client,self.root,store);app.account='me'
        with self.assertRaises(SpotifyError):app.recreate()
        self.assertEqual(store.read('state.json')['status'],'creating')
        self.assertEqual(store.read('replaced-target.json')['target'],'gone')
        client.request.reset_mock()
        with self.assertRaises(SpotifyError):app.recreate()
        client.request.assert_not_called()

    def test_recreate_does_not_treat_permission_failure_as_deletion(self):
        store=self.store();state=dict(status='ready',account='me',target='target',queue=[],history=[])
        store.write('state.json',state)
        client=Mock();client.request.side_effect=SpotifyError('Spotify HTTP 403',status=403)
        app=Application(client,self.root,store);app.account='me'
        with self.assertRaises(SpotifyError):app.recreate()
        self.assertEqual(store.read('state.json'),state)
        self.assertEqual(client.request.call_count,1)

    def test_removed_library_target_is_detected_before_source_import(self):
        from spotify_app.client import Client
        store=self.store();pool,state=fixture();store.write('state.json',state)
        client=Mock(spec=Client);client.playlist_saved.return_value=False
        app=Application(client,self.root,store);app.account=state['account']
        with self.assertRaises(SpotifyError) as caught:app.preview()
        self.assertEqual(caught.exception.ui_code,'target_missing')
        client.playlist.assert_not_called()
        client.source_playlist.assert_not_called()
