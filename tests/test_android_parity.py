"""Regression evidence ported from native sync, using synthetic Spotify only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from dataclasses import replace
from playlist_engine import Track
from spotify_app.sync import bootstrap,plan,dump
from spotify_app.service import Application
from spotify_app.storage import SQLiteStore
from spotify_app.writes import apply,PreconditionFailed,guard
from test_history_gap import fixture,event
from test_progressive import track

class Remote:
    def __init__(self,queue,events):self.queue=deepcopy(queue);self.snapshot=0;self.events=events;self.calls=[];self.fault=None;self.accepted=False
    def playlist(self,target):return dict(owner={'id':'me'},snapshot_id=str(self.snapshot)),[{'item':{'id':t['id']}} for t in self.queue]
    def recent(self):self.calls.append('recent');return self.events
    def write_batch(self,target,uris,append=False):
        kind='append' if append else 'replace';self.calls.append(kind)
        interrupt=self.fault==kind
        if interrupt and not self.accepted:self.fault=None;raise ConnectionError('lost')
        rows=[{'id':u.split(':')[-1]} for u in uris];self.queue=self.queue+rows if append else rows;self.snapshot+=1
        if interrupt:self.fault=None;raise ConnectionError('lost')

class NativeParityTests(unittest.TestCase):
    def test_required_history_does_not_delete_unheard_queue_or_source_removed_songs(self):
        pool,state=fixture();state['rules']=[dict(id='t',kind='track',window=100,strength='REQUIRED')]
        queue=deepcopy(state['queue']);play=event(1);play.update(track=track(queue[1]['id']),context={'uri':'spotify:playlist:other'})
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in queue],[event(0),play],{})
        self.assertEqual(updated['queue'],queue);self.assertFalse(report['write_needed'])
        updated,_=plan(state,[],state['snapshots'],[t['id'] for t in queue],[event(0)],{})
        self.assertEqual(updated['queue'],queue)

    def test_gap_consumes_only_observed_target_plays_without_dropping_unknown_prefix(self):
        pool,state=fixture();queue=deepcopy(state['queue']);plays=[event(i) for i in range(1,51)]
        plays[-1].update(track=track(queue[1]['id']),context={'uri':'spotify:playlist:target'})
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in queue],plays,{},allow_history_gap=True)
        self.assertTrue(report['history_gap']);self.assertFalse(report['consumption_complete']);self.assertEqual(report['consumed'],1)
        self.assertEqual([t['id'] for t in updated['queue'][:2]],[queue[0]['id'],queue[2]['id']])
        self.assertEqual(updated['last_event'][0],plays[-1]['played_at'])

    def test_distinct_recording_metadata_ambiguity_and_repeat_copy_protection(self):
        pool,state=fixture();q=state['queue'][0];state['queue']=[q,deepcopy(q)];state['size']=2
        plays=[event(0)]+[dict(event(i),track=track(q['id']),context={'uri':'spotify:playlist:target'}) for i in [1,2]]
        updated,report=plan(state,pool,state['snapshots'],[q['id'],q['id']],plays,{})
        self.assertEqual(report['consumed'],1);self.assertEqual(updated['queue'][0]['id'],q['id']);self.assertEqual(len(updated['history']),3)

    def test_target_substitution_matches_unique_exact_duration_and_title_only_in_target_context(self):
        pool,state=fixture();original=state['queue'][0]['id'];pool=[replace(t,name=' Song ',duration_ms=123000) if t.id==original else t for t in pool]
        play=dict(event(1),track=dict(track('alternate'),name='song',duration_ms=123000),context={'uri':'spotify:playlist:target'})
        _,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['playback_matches'][0]['method'],'unique_target_substitution')
        play['context']=None
        _,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],0)

    def test_ambiguous_isrc_does_not_select_first_recording(self):
        pool,state=fixture();queued={t['id'] for t in state['queue']};pool=[replace(t,recording_id='same') if t.id in queued else t for t in pool]
        play=dict(event(1),track=dict(track('alternate'),external_ids={'isrc':'same'}),context={'uri':'spotify:playlist:target'})
        _,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],0)

    def test_guard_does_not_request_or_freeze_playback(self):
        client=Mock();client.playlist.return_value=({'snapshot_id':'s'},[{'item':{'id':'one'}}]);client.recent.side_effect=AssertionError('No playback stability check')
        guard(client,'target','s',[],['one']);client.recent.assert_not_called()

    def test_idle_auto_check_reads_recent_only_and_keeps_last_meaningful_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=SQLiteStore(root)
            self.addCleanup(store.close);pool,state=fixture();state['last_full_check']=time.time();store.write('state.json',state);store.write('decision-log.json',{'old':'report'})
            client=Remote(state['queue'],[event(0)]);loader=Mock(side_effect=AssertionError('Idle check must not import'))
            app=Application(client,root,store,library_loader=loader);app.account='me';preview=app.preview(automatic=True);app.apply_preview(preview)
            self.assertEqual(client.calls,['recent']);self.assertEqual(store.read('decision-log.json'),{'old':'report'});loader.assert_not_called()

    def test_idle_real_client_interface_skips_library_membership_and_target_reads(self):
        from spotify_app.client import Client
        with tempfile.TemporaryDirectory() as folder:
            store=SQLiteStore(folder)
            try:
                _,state=fixture();state['last_full_check']=time.time();store.write('state.json',state)
                client=Mock(spec=Client);client.recent.return_value=[event(0)];client.playlist_saved.side_effect=AssertionError('Idle membership scan is unnecessary');client.playlist.side_effect=AssertionError('Idle target read is unnecessary')
                app=Application(client,folder,store);app.account='me';app.apply_preview(app.preview(automatic=True))
                client.recent.assert_called_once();client.playlist_saved.assert_not_called();client.playlist.assert_not_called()
            finally:store.close()

    def test_sqlite_replace_append_recovery_commits_history_once_for_all_four_failures(self):
        for kind in ['replace','append']:
            for accepted in [False,True]:
                with self.subTest(kind=kind,accepted=accepted),tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);store=SQLiteStore(root)
                    try:
                        pool=[Track(str(i),str(i),('a'+str(i),),'b'+str(i),('g',)) for i in range(300)]
                        state,_=bootstrap(pool,['source'],{'source':'v'},[event(0)],{},[],150 if kind=='append' else 30,0,'me')
                        state.update(status='ready',target='target');store.write('state.json',state);store.write('decision-log.json',{'old':'report'})
                        play=dict(event(1),track=track(state['queue'][0]['id']),context={'uri':'spotify:playlist:target'})
                        client=Remote(state['queue'],[event(0),play]);client.fault=kind;client.accepted=accepted
                        app=Application(client,root,store,library_loader=lambda *a,**k:(pool,state['snapshots'],0));app.account='me';preview=app.preview();expected=[t['id'] for t in preview.state['queue']]
                        with self.assertRaises(ConnectionError):app.apply_preview(preview)
                        pending=store.read('state.json');self.assertEqual(pending['history'],json.loads(json.dumps(state['history'])));self.assertEqual(pending['last_event'],state['last_event']);self.assertEqual(pending['queue'],json.loads(json.dumps(state['queue'])));self.assertEqual(store.read('decision-log.json'),{'old':'report'})
                        store.close();store=SQLiteStore(root);restarted=Application(client,root,store);restarted.account='me';restarted.recover_write()
                        confirmed=store.read('state.json');self.assertEqual([t['id'] for t in client.queue],expected);self.assertEqual(confirmed['last_event'],preview.state['last_event']);self.assertEqual(confirmed['history'],json.loads(json.dumps(preview.state['history'])));self.assertNotIn('pending_update',confirmed)
                        self.assertEqual(client.calls.count(kind),1 if accepted else 2)
                    finally:store.close()
