"""Dependency checks use saved mixes; Spotify playlists are read as flat snapshots."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from spotify_poc.service import Application
from spotify_poc.storage import SQLiteStore
from spotify_poc.client import SpotifyError
from spotify_poc.sync import library
from test_history_gap import fixture, event
from test_progressive import track

class DependencyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.database=self.root/'shared.sqlite'
        self.store=SQLiteStore(self.root/'downstream',self.database);self.addCleanup(self.store.close)
        self.client=Mock();self.client.request.return_value={'id':'me'}
        self.app=Application(self.client,self.store.root,self.store);self.app.connect()

    def mix(self,name,target,sources,account='me'):
        store=SQLiteStore(self.root/name,self.database);self.addCleanup(store.close)
        store.write('state.json',dict(account=account,target=target,sources=sources,history=[],queue=[]))

    def test_acyclic_chain_and_shared_diamond_are_allowed(self):
        self.mix('a','A',['B','C']);self.mix('b','B',['D']);self.mix('c','C',['D'])
        self.app.validate_dependencies(['A'],'downstream')

    def test_direct_self_reference_is_rejected_before_source_reads(self):
        _,state=fixture();state['sources']=['target'];self.store.write('state.json',state)
        with self.assertRaises(SpotifyError):self.app.preview()
        self.client.playlist.assert_not_called()

    def test_indirect_cycle_rejected_before_create(self):
        self.mix('a','A',['B']);self.mix('b','B',['C']);self.mix('c','C',['A'])
        self.client.reset_mock()
        with self.assertRaisesRegex(SpotifyError,'Circular'):self.app.create(sources=['A'],rows=[])
        self.client.request.assert_not_called()
        self.assertIsNone(self.store.read('setup.json'))

    def test_cycle_introduced_after_preview_is_rejected_before_write(self):
        pool,state=fixture();state['sources']=['A'];state['snapshots']={'A':'v1'}
        self.store.write('state.json',state);self.mix('a','A',['external'])
        self.app.library_loader=Mock(return_value=(pool,{'A':'v1'},0))
        self.client.playlist.return_value=({'owner':{'id':'me'},'snapshot_id':'v1'},[{'item':{'id':t['id']}} for t in state['queue']])
        self.client.recent.return_value=[event(0)]
        before=self.store.read('state.json');preview=self.app.preview();self.mix('a','A',['target'])
        with self.assertRaisesRegex(SpotifyError,'Circular'):self.app.apply_preview(preview)
        self.client.replace.assert_not_called()
        self.assertEqual(self.store.read('state.json'),before)

    def test_unrelated_or_other_account_cycle_does_not_block(self):
        self.mix('a','A',['A']);self.mix('other','B',['B'],account='other')
        self.app.validate_dependencies(['external','B'],'downstream')

    def test_upstream_snapshot_changes_refresh_downstream_and_deduplicate(self):
        _,state=fixture();state['sources']=['A','external'];state['snapshots']={'A':'v1','external':'v1'}
        self.store.write('state.json',state);self.mix('a','A',['original'])
        upstream=[track(i) for i in range(6)]
        def source(identity):
            return {'snapshot_id':'v2' if identity=='A' else 'v1'},[{'item':t} for t in (upstream if identity=='A' else [track(5),track(6)])]
        self.client.source_playlist.side_effect=source
        self.client.playlist.return_value=({'owner':{'id':'me'},'snapshot_id':'target-v1'},[{'item':{'id':t['id']}} for t in state['queue']])
        self.client.recent.return_value=[event(0)]
        pool,_,_=library(self.client,state['sources'],{},cache_sources=True)
        self.assertEqual(len(pool),7)
        preview=self.app.preview()
        self.assertTrue(preview.report['sources_changed'])
        self.assertEqual(preview.state['snapshots']['A'],'v2')
        self.client.replace.assert_not_called()
