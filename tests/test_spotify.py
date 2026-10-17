import unittest
from unittest.mock import Mock
from spotify_poc.client import Client, pkce, SpotifyError
from spotify_poc.sync import library, ordered, rules_from
from playlist_engine import Track


class SpotifyTests(unittest.TestCase):
    def test_batches(self):
        c=object.__new__(Client)
        c.request=Mock()
        c.replace('x',['u']*205)
        self.assertEqual([len(x.args[2]['uris']) for x in c.request.call_args_list],[100,100,5])

    def test_pkce(self):
        v,c=pkce()
        self.assertGreaterEqual(len(v),43)
        self.assertEqual(len(c),43)

    def test_tied_history(self):
        rows=[{'played_at':'1','track':{'id':'a'}}, {'played_at':'1','track':{'id':'b'}}]
        self.assertEqual(ordered(rows[::-1]),rows)
        with self.assertRaises(SpotifyError):ordered([rows[0],rows[0]])

    def test_missing_genres(self):
        with self.assertRaises(SpotifyError):
            rules_from([dict(id='g',kind='genre',window=1,strength='REQUIRED')],
                       [Track('x','x',('a',),'b',('__unknown__',))])

    def test_dedup(self):
        c=Mock()
        raw=dict(type='track',id='x',name='x',artists=[{'id':'a'}],album={'id':'b'})
        c.playlist.return_value=({'snapshot_id':'v'},[{'item':raw}])
        pool,_,_=library(c,['a','b'],{})
        self.assertEqual(len(pool),1)
        self.assertEqual(set(pool[0].sources),{'a','b'})

class SyncTests(unittest.TestCase):
    def setup_state(self):
        from spotify_poc.sync import bootstrap
        pool=[Track(str(i),str(i),('a'+str(i),),'b'+str(i),('g',)) for i in range(6)]
        state,_=bootstrap(pool,['s'],{'s':'1'},[],{},[],3,0,'me')
        state.update(status='ready',target='target')
        return pool,state

    def test_consumes_prefix(self):
        from spotify_poc.sync import plan
        pool,state=self.setup_state()
        first=state['queue'][0]['id']
        raw=dict(type='track',id=first,name=first,artists=[{'id':'a'+first}],album={'id':'b'+first})
        event=dict(track=raw,played_plat='1',context={'uri':'spotify:playlist:target'})
        event['played_at']='1'
        next_state,report=plan(state,pool,{'s':'1'},[t['id'] for t in state['queue']],[event],{})
        self.assertEqual(report['consumed'],1)
        self.assertEqual(len(next_state['queue']),3)
        self.assertEqual(len(next_state['history']),1)
        self.assertEqual(state['history'],[])

    def test_external_edit_stops(self):
        from spotify_poc.sync import plan
        pool,state=self.setup_state()
        with self.assertRaises(SpotifyError):
            plan(state,pool,{},[],[],{})
