from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from spotify_poc.client import SpotifyError, save_json, read_json
from spotify_poc.sync import recover_history, plan
from spotify_poc.__main__ import main
from test_history_gap import fixture, event


class HistoryRecoveryTests(unittest.TestCase):
    def test_new_baseline_preserves_queue_and_next_sync_works(self):
        pool,state=fixture()
        actual=[t['id'] for t in state['queue']]
        before=deepcopy(state)
        updated,report=recover_history(state,pool,{'source':'v1'},actual,[event(i) for i in range(1,51)],{})
        self.assertEqual(state,before)
        self.assertEqual(updated['queue'],state['queue'])
        self.assertFalse(report['write_needed'])
        self.assertFalse(report['history_recovery']['consumption_known'])
        self.assertEqual(len(updated['history']),50)
        self.assertNotIn(state['history'][0],updated['history'])
        after,report=plan(updated,pool,{'source':'v1'},actual,[event(i) for i in range(2,52)],{})
        self.assertEqual(report['new_plays'],1)
        self.assertEqual(after['last_event'][0],event(51)['played_at'])

    def test_recent_target_play_is_history_without_guessing_consumption(self):
        pool,state=fixture()
        state['rules']=[dict(id='track',kind='track',window=100,strength='REQUIRED')]
        actual=[t['id'] for t in state['queue']]
        played=pool[next(i for i,t in enumerate(pool) if t.id==actual[0])]
        play=event(1)
        play['track']=dict(type='track',id=played.id,name=played.name,
                           artists=[{'id':a} for a in played.artists],album={'id':played.album})
        play['context']={'uri':'spotify:playlist:target'}
        updated,report=recover_history(state,pool,{'source':'v1'},actual,[play],{})
        self.assertEqual(updated['history'][-1]['id'],played.id)
        self.assertEqual(updated['queue'],state['queue'])
        self.assertEqual(report['history_recovery']['insufficient_history_rules'],['track'])
        self.assertNotIn('consumed',report)

    def test_invalid_baselines_and_pending_writes_stop(self):
        for mode in ('empty','present','stale','unsupported','pending','ties'):
            with self.subTest(mode=mode):
                pool,state=fixture();events=[event(1)]
                if mode=='empty':events=[]
                if mode=='present':events=[event(0),event(1)]
                if mode=='stale':events=[dict(event(1),played_at='2026-10-07T00:00:00Z')]
                if mode=='unsupported':events[0]['track']={'type':'episode'}
                if mode=='pending':state['status']='pending'
                if mode=='ties':events=[event(1),event(1)]
                before=deepcopy(state)
                with self.assertRaises(SpotifyError):
                    recover_history(state,pool,{'source':'v1'},[t['id'] for t in state['queue']],events,{})
                self.assertEqual(state,before)

    def test_impossible_required_rules_preserve_old_state(self):
        pool,state=fixture();state['rules']=[dict(id='genre',kind='genre',window=1,strength='REQUIRED')]
        play=event(1);play['track']['id']='recent'
        before=deepcopy(state)
        updated,report=recover_history(state,pool,{'source':'v1'},[t['id'] for t in state['queue']],[play],{'recent':['g']})
        self.assertEqual(updated['queue'],state['queue'])
        self.assertFalse(report['write_needed'])
        self.assertEqual(state,before)

    def test_cli_preview_changes_nothing_apply_backs_up_and_verifies(self):
        for apply_flag in (False,True):
            with self.subTest(apply=apply_flag),tempfile.TemporaryDirectory() as folder:
                pool,state=fixture();root=Path(folder);path=root/'state.json';save_json(path,state)
                previous=path.read_bytes()
                client=Mock();client.metrics={};client.request.return_value={'id':'me','snapshot_id':'v1'}
                client.playlist.return_value=({'owner':{'id':'me'},'snapshot_id':'v1'},
                     [{'item':{'id':t['id']}} for t in state['queue']])
                client.recent.return_value=[event(1)]
                args=['spotify_poc','--accounts-directory',str(Path(folder)/'accounts'),'--storage','json','--directory',folder,'recover-history']+(['--apply'] if apply_flag else [])
                with patch('sys.argv',args),patch('spotify_poc.__main__.Client',return_value=client), \
                     patch('spotify_poc.__main__.library',return_value=(pool,{'source':'v1'},0)),patch('builtins.print'):
                    main()
                client.replace.assert_not_called()
                if apply_flag:
                    self.assertEqual(read_json(path)['last_event'][0],event(1)['played_at'])
                    self.assertEqual(read_json(path)['status'],'ready')
                    self.assertEqual(read_json(root/'history-backup-0.json'),json.loads(previous))
                else:
                    self.assertEqual(path.read_bytes(),previous)
                    self.assertFalse((root/'history-backup-0.json').exists())

    def test_cli_target_changed_before_apply_does_not_save_new_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            pool,state=fixture();root=Path(folder);path=root/'state.json';save_json(path,state)
            saved=path.read_bytes()
            client=Mock();client.metrics={};client.request.side_effect=[{'id':'me'},{'snapshot_id':'changed'}]
            client.playlist.return_value=({'owner':{'id':'me'},'snapshot_id':'v1'},
                [{'item':{'id':t['id']}} for t in state['queue']])
            client.recent.return_value=[event(1)]
            initial_rows=client.playlist.return_value[1]
            client.playlist.side_effect=[client.playlist.return_value,({'owner':{'id':'me'},'snapshot_id':'changed'},initial_rows)]
            with patch('sys.argv',['spotify_poc','--accounts-directory',str(Path(folder)/'accounts'),'--storage','json','--directory',folder,'recover-history','--apply']), \
                 patch('spotify_poc.__main__.Client',return_value=client), \
                 patch('spotify_poc.__main__.library',return_value=(pool,{'source':'v1'},0)),patch('builtins.print'):
                with self.assertRaisesRegex(SpotifyError,'Target changed'):
                    main()
            self.assertEqual(path.read_bytes(),saved)
            client.replace.assert_not_called()

    def test_interrupted_recovery_write_is_journaled_and_can_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            pool,state=fixture()
            state['rules']=[];state['size']=4
            root=Path(folder);path=root/'state.json';save_json(path,state)
            old=read_json(path)
            first=next(t for t in pool if t.id==state['queue'][0]['id'])
            play=event(1)
            play['track']=dict(type='track',id=first.id,name=first.name,
                artists=[{'id':a} for a in first.artists],album={'id':first.album})
            client=Mock();client.metrics={};client.request.return_value={'id':'me','snapshot_id':'v1'}
            metadata={'owner':{'id':'me'},'snapshot_id':'v1'}
            client.playlist.return_value=(metadata,[{'item':{'id':t['id']}} for t in state['queue']])
            client.recent.return_value=[play]
            client.replace.side_effect=SpotifyError('Network failure; outcome unknown')
            with patch('sys.argv',['spotify_poc','--accounts-directory',str(Path(folder)/'accounts'),'--storage','json','--directory',folder,'recover-history','--apply']), \
                 patch('spotify_poc.__main__.Client',return_value=client), \
                 patch('spotify_poc.__main__.library',return_value=(pool,{'source':'v1'},0)),patch('builtins.print'):
                with self.assertRaisesRegex(SpotifyError,'outcome unknown'):
                    main()
            pending=read_json(path)
            self.assertEqual(pending['status'],'pending')
            self.assertEqual(read_json(root/'history-backup-0.json'),old)
            self.assertEqual(pending['last_event'],old['last_event'])
            self.assertEqual(pending['pending_update']['last_event'][0],play['played_at'])
            self.assertEqual(client.replace.call_count,1)
            # Model readback after an uncertain write that actually succeeded remotely.
            client.replace.reset_mock();client.replace.side_effect=None
            client.playlist.return_value=(metadata,[{'item':{'id':t['id']}} for t in pending['pending_update']['queue']])
            with patch('sys.argv',['spotify_poc','--accounts-directory',str(Path(folder)/'accounts'),'--storage','json','--directory',folder,'recover','--apply']), \
                 patch('spotify_poc.__main__.Client',return_value=client):
                main()
            client.replace.assert_not_called()
            ready=read_json(path)
            self.assertEqual(ready['status'],'ready')
            _,report=plan(ready,pool,{'source':'v1'},[t['id'] for t in ready['queue']],[play],{})
            self.assertEqual(report['new_plays'],0)
