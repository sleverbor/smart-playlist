import test_ui
import unittest
from unittest.mock import patch
import time

class MixControlTests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    new_controller=test_ui.UITests.new_controller
    wait=test_ui.UITests.wait
    create=test_ui.UITests.create
    def test_queue_and_rule_edit_invalidate_preview(self):
        mix=self.create()['data']['mix']
        detail=self.controller.details(mix)
        self.assertEqual(len(detail['queue']),8)
        old=self.wait(self.controller.submit('preview',dict(mix=mix)))
        rules=[dict(id='track',kind='track',window=20,strength='REQUIRED')]
        saved=self.wait(self.controller.submit('rules',dict(mix=mix,rules=rules)))
        self.assertEqual(saved['status'],'complete')
        self.assertEqual(self.controller.details(mix)['rules'],rules)
        applied=self.wait(self.controller.submit('apply',dict(mix=mix,preview_id=old['data']['preview_id'])))
        self.assertEqual(applied['status'],'failed')

    def test_single_sync_and_opt_in_scheduling(self):
        mix=self.create()['data']['mix']
        sync=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(sync['status'],'complete',sync)
        with patch.object(self.controller,'submit') as submit:
            self.controller.schedule_due(time.time()+2000)
            submit.assert_not_called()
        self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=True)))
        now=time.time()+2000
        with patch.object(self.controller,'submit') as submit:
            self.controller.schedule_due(now)
            submit.assert_called_once_with('sync',dict(mix=mix,automatic=True))
            self.controller.schedule_due(now+1)
            self.assertEqual(submit.call_count,1)
        self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=False)))
        with patch.object(self.controller,'submit') as submit:
            self.controller.schedule_due(now+4000)
            submit.assert_not_called()

    def test_rule_validation_preserves_existing_rules(self):
        mix=self.create()['data']['mix'];before=self.controller.details(mix)['rules']
        job=self.wait(self.controller.submit('rules',dict(mix=mix,rules=[dict(id='track',kind='track',window=-1,strength='REQUIRED')])))
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.controller.details(mix)['rules'],before)

    def test_sync_status_and_schedule_survive_restart(self):
        mix=self.create()['data']['mix']
        self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=True)))
        before=self.controller.snapshot()['mixes'][0]
        self.assertIsNotNone(before['last_sync'])
        self.assertEqual(before['sync_status'],'complete')
        self.controller.close()
        self.controller=self.new_controller()
        self.addCleanup(self.controller.close)
        after=self.controller.snapshot()['mixes'][0]
        self.assertEqual(after['last_sync'],before['last_sync'])
        with patch.object(self.controller,'submit') as submit:
            self.controller.schedule_due(after['next_sync']+1)
            submit.assert_called_once_with('sync',dict(mix=mix,automatic=True))

    def test_unavailable_or_expired_login_pauses_automatic_sync(self):
        from spotify_app.client import SpotifyError
        for code in (401,404):
            mix=self.create()['data']['mix']
            self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=True)))
            with patch('spotify_app.service.Application.preview',side_effect=SpotifyError(f'Spotify HTTP {code}')):
                failed=self.wait(self.controller.submit('sync',dict(mix=mix)))
            self.assertEqual(failed['status'],'failed')
            saved=next(m for m in self.controller.snapshot()['mixes'] if m['id']==mix)
            self.assertFalse(saved['auto_sync'])
            self.assertEqual(saved['sync_status'],'failed')
            self.assertIsNone(saved['last_sync'])
            self.assertTrue(saved['sync_message'])

    def test_cooldown_skips_scheduled_jobs_then_resumes_at_deadline(self):
        mix=self.create()['data']['mix']
        self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=True)))
        now=time.time()+2000;deadline=now+3600
        with patch.object(self.controller,'cooldown_until',return_value=deadline),patch.object(self.controller,'submit') as submit,patch('spotify_app.client.http_json',side_effect=AssertionError('no requests during cooldown')):
            self.controller.schedule_due(now)
            self.controller.schedule_due(now+600)
            submit.assert_not_called()
            saved=self.controller.snapshot()['mixes'][0]
            self.assertEqual(saved['next_sync'],deadline)
            self.assertEqual(saved['cooldown_until'],deadline)
            self.controller.schedule_due(deadline)
            submit.assert_called_once_with('sync',dict(mix=mix,automatic=True))

    def test_manual_sync_during_cooldown_does_not_connect(self):
        mix=self.create()['data']['mix']
        with patch.object(self.controller,'cooldown_until',return_value=time.time()+3600),patch.object(self.controller,'client_factory',side_effect=AssertionError('must not connect')):
            job=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(job['status'],'failed')
        self.assertIn('Waiting for Spotify until',job['error'])

    def test_edit_sources_preserves_buffer_and_invalidates_preview(self):
        from spotify_app.storage import SQLiteStore
        mix=self.create()['data']['mix'];before=self.controller.details(mix)['queue']
        old=self.wait(self.controller.submit('preview',dict(mix=mix)))
        with patch('spotify_app.client.http_json',side_effect=AssertionError('offline edit')):
            job=self.wait(self.controller.submit('edit_sources',dict(mix=mix,sources=['SourceB','SourceC','SourceB'])))
        self.assertEqual(job['status'],'complete',job)
        detail=self.controller.details(mix)
        self.assertEqual(detail['sources'],['SourceB','SourceC'])
        self.assertEqual(detail['queue'],before)
        stale=self.wait(self.controller.submit('apply',dict(mix=mix,preview_id=old['data']['preview_id'])))
        self.assertEqual(stale['status'],'failed')
        sync=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(sync['status'],'complete',sync)
        store=SQLiteStore(self.controller.workspace(mix),self.controller.database)
        try:store.write('activity.json',[dict(action='sync',status='complete',timestamp=i) for i in range(20)])
        finally:store.close()
        self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=False)))
        self.assertEqual(len(self.controller.details(mix)['activity']),20)

    def test_source_edit_rejects_empty_and_generated_preserving_sources(self):
        from spotify_app.storage import SQLiteStore
        mix=self.create()['data']['mix'];before=self.controller.details(mix)['sources']
        store=SQLiteStore(self.controller.workspace(mix),self.controller.database)
        try:store.db.execute('INSERT INTO generated_targets VALUES(?,?)',('test-account','SourceC'));store.db.commit()
        finally:store.close()
        for sources in ([],['SourceC'],['../bad']):
            job=self.wait(self.controller.submit('edit_sources',dict(mix=mix,sources=sources)))
            self.assertEqual(job['status'],'failed',job)
            self.assertEqual(self.controller.details(mix)['sources'],before)

    def test_mix_settings_are_offline_then_sync_renames_and_resizes(self):
        mix=self.create()['data']['mix'];before=self.controller.details(mix)['queue']
        old=self.wait(self.controller.submit('preview',dict(mix=mix)))
        with patch('spotify_app.client.http_json',side_effect=AssertionError('offline edit')):
            edited=self.wait(self.controller.submit('edit_mix',dict(mix=mix,name='New name',size=3)))
        self.assertEqual(edited['status'],'complete',edited)
        self.assertEqual(self.controller.details(mix)['queue'],before)
        self.assertEqual(self.controller.details(mix)['name'],'New name')
        stale=self.wait(self.controller.submit('apply',dict(mix=mix,preview_id=old['data']['preview_id'])))
        self.assertEqual(stale['status'],'failed')
        sync=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(sync['status'],'complete',sync)
        self.assertEqual(self.controller.details(mix)['queue'],before[:3])
        self.assertIn('New name',self.fake.names.values())
        self.wait(self.controller.submit('edit_mix',dict(mix=mix,name='New name',size=10)))
        sync=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(sync['status'],'complete',sync)
        self.assertEqual(len(self.controller.details(mix)['queue']),10)

    def test_invalid_mix_settings_preserve_saved_mix(self):
        mix=self.create()['data']['mix'];before=self.controller.details(mix)
        for name,size in [('',3),('x'*101,3),('Name',True),('Name',0),('Name',10001)]:
            job=self.wait(self.controller.submit('edit_mix',dict(mix=mix,name=name,size=size)))
            self.assertEqual(job['status'],'failed',job)
            after=self.controller.details(mix)
            self.assertEqual((after['name'],after['size'],after['queue']),(before['name'],before['size'],before['queue']))

    def test_pending_changes_survive_restart_and_failed_sync(self):
        mix=self.create()['data']['mix'];before=self.controller.details(mix)['queue']
        edited=self.wait(self.controller.submit('edit_mix',dict(mix=mix,name='Restarted mix',size=5)))
        self.assertEqual(edited['status'],'complete')
        fake=self.fake
        self.controller.close()
        self.controller=self.new_controller()
        self.addCleanup(self.controller.close)
        row=self.controller.snapshot()['mixes'][0]
        self.assertTrue(row['changes_pending'])
        self.assertEqual((row['name'],row['normal']),('Restarted mix',5))
        self.assertEqual(self.controller.details(mix)['queue'],before)
        with patch.object(self.controller,'connect',side_effect=RuntimeError('Offline')):
            failed=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(self.controller.snapshot()['mixes'][0]['changes_pending'])
        sync=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(sync['status'],'complete',sync)
        self.assertFalse(self.controller.snapshot()['mixes'][0]['changes_pending'])
        self.assertEqual(self.controller.details(mix)['queue'],before[:5])
        self.assertIn('Restarted mix',fake.names.values())

    def test_recreate_deleted_target_preserves_mix_and_refuses_existing(self):
        from spotify_app.client import SpotifyError
        from spotify_app.storage import SQLiteStore
        mix=self.create()['data']['mix'];before=self.controller.details(mix)
        refused=self.wait(self.controller.submit('recreate',dict(mix=mix)))
        self.assertEqual(refused['status'],'failed')
        self.assertEqual(len(self.fake.targets),1)
        target=self.controller.snapshot()['mixes'][0]['target']
        request=self.fake.request;playlist=self.fake.playlist
        def missing_request(method,path,data=None):
            if method=='GET' and path=='/playlists/'+target:raise SpotifyError('Spotify HTTP 404',status=404)
            return request(method,path,data)
        def missing_playlist(identity):
            if identity==target:raise SpotifyError('Spotify HTTP 404',status=404)
            return playlist(identity)
        with patch.object(self.fake,'request',side_effect=missing_request),patch.object(self.fake,'playlist',side_effect=missing_playlist):
            failed=self.wait(self.controller.submit('preview',dict(mix=mix)))
            self.assertEqual(failed['error_detail']['code'],'target_missing')
            self.assertEqual(self.controller.snapshot()['mixes'][0]['sync_error']['code'],'target_missing')
            recovered=self.wait(self.controller.submit('recreate',dict(mix=mix)))
        self.assertEqual(recovered['status'],'complete',recovered)
        after=self.controller.details(mix)
        for field in ('name','sources','rules','size','queue'):self.assertEqual(after[field],before[field])
        self.assertNotEqual(self.controller.snapshot()['mixes'][0]['target'],target)
        self.assertEqual(self.controller.snapshot()['mixes'][0]['sync_status'],'complete')

    def test_two_due_mixes_both_run_without_overlapping(self):
        first=self.create()['data']['mix']
        second=self.create()['data']['mix']
        for mix in (first,second):
            self.wait(self.controller.submit('settings',dict(mix=mix,auto_sync=True)))
        now=time.time()+2000
        with patch.object(self.controller,'submit') as submit:
            self.controller.schedule_due(now)
            self.assertEqual(submit.call_count,1)
            self.controller.schedule_due(now+1)
            self.assertEqual(submit.call_count,2)
            self.assertEqual({call.kwargs.get('mix') or call.args[1]['mix'] for call in submit.call_args_list},{first,second})
            self.controller.schedule_due(now+2)
            self.assertEqual(submit.call_count,2)

    def test_sources_and_rules_edits_survive_restart_then_sync(self):
        mix=self.create()['data']['mix']
        before=self.controller.details(mix)['queue']
        rules=[dict(id='track',kind='track',window=20,strength='REQUIRED')]
        self.wait(self.controller.submit('rules',dict(mix=mix,rules=rules)))
        self.wait(self.controller.submit('edit_sources',dict(mix=mix,sources=['SourceB','SourceC'])))
        self.controller.close()
        self.controller=self.new_controller()
        self.addCleanup(self.controller.close)
        saved=self.controller.snapshot()['mixes'][0]
        self.fake.targets[saved['target']]=[int(row['id'].split('Track')[-1]) for row in before]
        self.fake.version[saved['target']]=1
        detail=self.controller.details(mix)
        self.assertEqual(detail['sources'],['SourceB','SourceC'])
        self.assertEqual(detail['rules'],rules)
        self.assertEqual(detail['queue'],before)
        synced=self.wait(self.controller.submit('sync',dict(mix=mix)))
        self.assertEqual(synced['status'],'complete',synced)
        self.assertEqual(len(self.controller.details(mix)['queue']),8)
