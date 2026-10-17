"""Playback bookmarks must establish continuity before queue consumption."""
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from playlist_engine import Track
from spotify_poc.__main__ import main
from spotify_poc.client import SpotifyError, save_json
from spotify_poc.sync import bootstrap, plan


def event(index):
    return dict(played_at=f'2026-10-08T00:{index // 60:02d}:{index % 60:02d}Z',
                track=dict(type='track',id='external'+str(index),name='external',
                           artists=[{'id':'artist'}],album={'id':'album'}),context=None)


def fixture():
    pool=[Track(str(i),str(i),('a'+str(i),),'b'+str(i),('g',)) for i in range(6)]
    state,_=bootstrap(pool,['source'],{'source':'v1'},[event(0)],{},[],3,0,'me')
    state.update(status='ready',target='target')
    return pool,state


class HistoryGapTests(unittest.TestCase):
    def test_missing_bookmark_in_full_recent_window_preserves_inputs(self):
        pool,state=fixture()
        events=[event(i) for i in range(1,51)]
        before=deepcopy((pool,state,events))
        with patch('spotify_poc.sync.generate') as generate:
            with self.assertRaisesRegex(SpotifyError,'Playback history gap'):
                plan(state,pool,{'source':'v1'},[t['id'] for t in state['queue']],events,{})
            generate.assert_not_called()
        self.assertEqual((pool,state,events),before)

    def test_empty_history_with_existing_bookmark_also_stops(self):
        pool,state=fixture()
        before=deepcopy(state)
        with self.assertRaisesRegex(SpotifyError,'Playback history gap'):
            plan(state,pool,{'source':'v1'},[t['id'] for t in state['queue']],[],{})
        self.assertEqual(state,before)

    def test_cli_gap_reports_unknown_consumption_without_implicit_writes(self):
        for flags in ([],['--apply'],['--reconcile'],['--apply','--reconcile']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as folder:
                pool,state=fixture()
                root=Path(folder)
                save_json(root/'state.json',state)
                save_json(root/'decision-log.json',{'existing':'report'})
                saved={name:(root/name).read_bytes() for name in ('state.json','decision-log.json')}
                client=Mock()
                client.metrics={}
                client.request.return_value={'id':'me'}
                # An edited target exercises --reconcile's path, which must still require the bookmark.
                rows=[] if '--reconcile' in flags else [{'item':{'id':t['id']}} for t in state['queue']]
                client.playlist.return_value=({'snapshot_id':'target-v1','owner':{'id':'me'}},rows)
                client.recent.return_value=[event(i) for i in range(1,51)]
                with patch('spotify_poc.__main__.Client',return_value=client), \
                     patch('spotify_poc.__main__.library',return_value=(pool,{'source':'v1'},0)), \
                     patch('spotify_poc.__main__.apply') as apply, \
                     patch('sys.argv',['spotify_poc','--accounts-directory',str(Path(folder)/'accounts'),'--storage','json','--directory',folder,'sync']+flags):
                    with patch('builtins.print'):
                        main()
                    self.assertEqual(apply.call_count,int('--apply' in flags))
                client.replace.assert_not_called()
                self.assertEqual([call.args[0] for call in client.request.call_args_list],['GET'])
                if '--apply' not in flags:
                    for name,contents in saved.items():self.assertEqual((root/name).read_bytes(),contents)

    def test_bookmark_at_oldest_retained_event_allows_sync(self):
        pool,state=fixture()
        before=deepcopy(state)
        updated,report=plan(state,pool,{'source':'v1'},[t['id'] for t in state['queue']],
                            [event(i) for i in range(50)],{})
        self.assertEqual(report['new_plays'],49)
        self.assertEqual(report['consumed'],0)
        self.assertEqual(updated['last_event'][0],event(49)['played_at'])
        self.assertEqual(state,before)
