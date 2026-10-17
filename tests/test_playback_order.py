import unittest
from copy import deepcopy
from spotify_poc.sync import plan
from test_history_gap import fixture, event
from test_progressive import track

class PlaybackOrderTests(unittest.TestCase):
    def play(self, index, identity, target=True):
        row=event(index);row['track']=track(identity)
        row['context']={'uri':'spotify:playlist:target' if target else 'spotify:playlist:elsewhere'}
        return row

    def run_plan(self, plays):
        pool,state=fixture();before=deepcopy(state)
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0)]+plays,{})
        self.assertEqual(state,before)
        return state,updated,report

    def test_shuffle_removes_only_observed_track_and_keeps_unheard_prefix(self):
        _,state=fixture();ids=[t['id'] for t in state['queue']]
        _,updated,report=self.run_plan([self.play(1,ids[2])])
        self.assertEqual(report['consumed'],1)
        self.assertEqual(report['reordered_plays'],1)
        self.assertEqual([t['id'] for t in updated['queue'][:2]],ids[:2])
        self.assertEqual(updated['history'][-1]['id'],ids[2])

    def test_repeat_records_each_play_without_consuming_another_song(self):
        _,state=fixture();identity=state['queue'][0]['id']
        _,updated,report=self.run_plan([self.play(1,identity),self.play(2,identity)])
        self.assertEqual(report['consumed'],1)
        self.assertEqual(report['repeated_or_unqueued_plays'],1)
        self.assertEqual([t['id'] for t in updated['history'][-2:]],[identity,identity])

    def test_unqueued_target_track_is_history_without_guessed_consumption(self):
        _,_,report=self.run_plan([self.play(1,'outside')])
        self.assertEqual(report['consumed'],0)
        self.assertEqual(report['repeated_or_unqueued_plays'],1)

    def test_other_playlist_play_does_not_consume_target_queue(self):
        _,state=fixture();_,updated,report=self.run_plan([self.play(1,state['queue'][1]['id'],False)])
        self.assertEqual(report['consumed'],0)
        self.assertEqual(updated['history'][-1]['id'],state['queue'][1]['id'])

    def test_shuffled_play_respects_required_track_cooldown(self):
        pool,state=fixture();identity=state['queue'][2]['id']
        state['rules']=[dict(id='track',kind='track',window=100,strength='REQUIRED')]
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),self.play(1,identity)],{})
        self.assertNotIn(identity,[t['id'] for t in updated['queue']])
        self.assertEqual(report['consumed'],1)

    def test_distinct_tied_timestamps_are_stable_and_preserve_all_plays(self):
        from spotify_poc.sync import ordered
        a=self.play(1,'a');b=self.play(1,'b')
        self.assertEqual(ordered([a,b]),ordered([b,a]))
        self.assertEqual(len(ordered([a,b])),2)
        pool,state=fixture()
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),b,a],{})
        self.assertEqual(report['new_plays'],2)
        self.assertEqual([t['id'] for t in updated['history'][-2:]],['a','b'])


class RecordingIdentityTests(unittest.TestCase):
    def test_matching_recording_consumes_alternate_and_enforces_cooldown(self):
        from dataclasses import replace
        from spotify_poc.sync import dump
        pool,state=fixture()
        original=state['queue'][0]['id']
        pool=[replace(t,recording_id='ISRC123') if t.id==original else t for t in pool]
        # Saved queues created before metadata fields existed must still work.
        play=event(1);play['track']=track('alternate');play['track']['external_ids']={'isrc':'ISRC123'}
        play['context']={'uri':'spotify:playlist:target'}
        play['track']['artists']=[{'id':a} for a in next(t for t in pool if t.id==original).artists]
        state['rules']=[dict(id='track',kind='track',window=100,strength='REQUIRED')]
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],1)
        self.assertNotIn(original,[t['id'] for t in updated['queue']])
        self.assertEqual(updated['history'][-1]['id'],'alternate')

    def test_linked_from_matches_without_isrc(self):
        pool,state=fixture();original=state['queue'][0]['id']
        play=event(1);play['track']=track('alternate');play['track']['linked_from']={'id':original}
        play['context']={'uri':'spotify:playlist:target'}
        _,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],1)

    def test_same_title_different_recording_is_not_matched(self):
        from spotify_poc.sync import adapt,same_recording
        a=track('a');b=track('b');a['name']=b['name']='Same title'
        a['external_ids']={'isrc':'A'};b['external_ids']={'isrc':'B'}
        self.assertFalse(same_recording(adapt(a,{}),adapt(b,{})))


class ContextualPlaybackTests(unittest.TestCase):
    def test_unique_target_version_match_keeps_observed_history_id(self):
        from dataclasses import replace
        pool,state=fixture();original=state['queue'][0]['id']
        pool=[replace(t,name='Song',duration_ms=257959,recording_id='source-isrc') if t.id==original else t for t in pool]
        play=event(1);play['track']=track('alternate');play['track'].update(name='Song',duration_ms=257932,external_ids={'isrc':'other-isrc'})
        play['context']={'uri':'spotify:playlist:target'}
        play['track']['artists']=[{'id':a} for a in next(t for t in pool if t.id==original).artists]
        state['rules']=[dict(id='track',kind='track',window=100,strength='REQUIRED')]
        updated,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],1)
        self.assertEqual(report['playback_matches'][0]['method'],'unique_target_metadata')
        self.assertNotIn(original,[t['id'] for t in updated['queue']])
        self.assertEqual(updated['history'][-1]['id'],'alternate')

    def test_ambiguous_versions_are_not_consumed(self):
        from dataclasses import replace
        pool,state=fixture();queued={t['id'] for t in state['queue']}
        pool=[replace(t,name='Song',duration_ms=200000) if t.id in queued else t for t in pool]
        play=event(1);play['track']=track('alternate');play['track'].update(name='Song',duration_ms=200000)
        # fixture artists may differ; use one common artist to make evidence ambiguous.
        pool=[replace(t,artists=('a',)) for t in pool];play['track']['artists']=[{'id':'a'}]
        play['context']={'uri':'spotify:playlist:target'}
        _,report=plan(state,pool,state['snapshots'],[t['id'] for t in state['queue']],[event(0),play],{})
        self.assertEqual(report['consumed'],0)
