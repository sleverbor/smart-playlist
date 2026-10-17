import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from spotify_poc.progressive import run, confidence
from spotify_poc.__main__ import apply, ids
from spotify_poc.client import SpotifyError, read_json, save_json
from playlist_engine import Track


def track(i, artist=None):
    return dict(type='track', id=str(i), name=str(i),
                artists=[{'id':artist or 'a'+str(i)}], album={'id':'b'+str(i)})


class FakeSpotify:
    def __init__(self, sources=3, pages=3, artists=None):
        self.sources={str(s): [[track(s*1000+p*10+i, artists) for i in range(10)]
                              for p in range(pages)] for s in range(sources)}
        self.snapshots={s:'v1' for s in self.sources}
        self.target=[]
        self.target_snapshot=0
        self.reads=[]
        self.creates=[]
        self.events=[]
        self.fail_page=None

    def request(self, method, path, data=None):
        if path=='/me':
            return {'id':'me'}
        if method=='POST':
            self.creates.append(len(self.reads))
            return {'id':'target'}
        parts=path.split('/')
        identity=parts[2]
        if identity=='target':
            return {'snapshot_id':str(self.target_snapshot),'owner':{'id':'me'}}
        if '/items' not in path:
            return {'snapshot_id':self.snapshots[identity],'owner':{'id':'me'}}
        page=int(path.split('page=')[-1]) if 'page=' in path else 0
        if (identity,page)==self.fail_page:
            raise SpotifyError('simulated offline')
        self.reads.append((identity,page))
        pages=self.sources[identity]
        return dict(items=[{'item':t} for t in pages[page]], total=sum(map(len,pages)),
                    next=f'/playlists/{identity}/items?page={page+1}' if page+1<len(pages) else None)

    def recent(self):
        return self.events

    def playlist(self, identity):
        return self.request('GET','/playlists/'+identity), [{'item':track(i)} for i in self.target]

    def write_batch(self,identity,uris,append=False):
        if not append:return self.replace(identity,uris)
        self.target.extend(u.split(':')[-1] for u in uris);self.target_snapshot+=1

    def replace(self, identity, uris):
        self.target=[u.split(':')[-1] for u in uris]
        self.target_snapshot+=1


class ProgressiveTests(unittest.TestCase):
    def start(self, client, root, **kwargs):
        run(client,root,'me',apply,ids,sources=list(client.sources),
            rows=[dict(id='t',kind='track',window=10,strength='REQUIRED')],
            minimum=2,normal=6,**kwargs)

    def test_early_publish_and_round_robin(self):
        c=FakeSpotify()
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit') as progress:
            self.start(c,Path(d))
            self.assertEqual(c.creates,[3])
            self.assertEqual(c.reads[:3],[('0',0),('1',0),('2',0)])
            self.assertEqual(len(c.target),6)
            self.assertEqual(read_json(Path(d)/'setup.json')['status'],'complete')
            self.assertTrue(any('Ready to play' in call.args[0] for call in progress.call_args_list))
            self.assertTrue(any('Building initial playlist: 1/2' in call.args[0] for call in progress.call_args_list))

    def test_cancel_after_ready_and_resume(self):
        c=FakeSpotify()
        def interrupt(message):
            if 'Ready to play' in message:
                raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch('spotify_poc.progressive.emit',side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                self.start(c,root)
            self.assertEqual(len(c.target),2)
            with patch('spotify_poc.progressive.emit'):
                run(c,root,'me',apply,ids)
            self.assertEqual(len(c.creates),1)
            self.assertEqual(len(c.target),6)
            self.assertEqual(len(c.reads),9)

    def test_impossible_artist_rule_never_creates(self):
        c=FakeSpotify(artists='same')
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            with self.assertRaises(SpotifyError):
                run(c,Path(d),'me',apply,ids,sources=list(c.sources),
                    rows=[dict(id='a',kind='artist',window=50,strength='REQUIRED')],minimum=2,normal=6)
        self.assertEqual(c.creates,[])

    def test_duplicate_sources_do_not_inflate_reserve(self):
        c=FakeSpotify()
        c.sources={s:[[track('same')]*10]*3 for s in c.sources}
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            with self.assertRaises(SpotifyError):
                self.start(c,Path(d))
        self.assertEqual(c.creates,[])

    def test_genre_missing_blocks(self):
        c=FakeSpotify()
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            with self.assertRaisesRegex(SpotifyError,'Genre'):
                run(c,Path(d),'me',apply,ids,sources=list(c.sources),
                    rows=[dict(id='g',kind='genre',window=1,strength='REQUIRED')],minimum=2,normal=6)
        self.assertEqual(c.creates,[])

    def test_failed_import_preserves_playable_buffer(self):
        c=FakeSpotify()
        c.fail_page=('0',1)
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            with self.assertRaises(SpotifyError):
                self.start(c,Path(d))
            self.assertEqual(len(c.target),2)
            self.assertEqual(read_json(Path(d)/'state.json')['status'],'ready')
            c.fail_page=None
            run(c,Path(d),'me',apply,ids)
            self.assertEqual(len(c.creates),1)

    def test_reserve_derived_from_required_window(self):
        pool=[Track(str(i),str(i),('a',),'b',('g',)) for i in range(100)]
        ready,evidence=confidence(pool,[dict(id='a',kind='artist',window=50,strength='REQUIRED')],20,False)
        self.assertFalse(ready)
        self.assertEqual(evidence['requirements']['artist'],51)

    def test_pending_write_requires_recovery(self):
        c=FakeSpotify()
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            root=Path(d)
            c.fail_page=('0',1)
            with self.assertRaises(SpotifyError):
                self.start(c,root)
            state=read_json(root/'state.json')
            state['status']='pending'
            save_json(root/'state.json',state)
            with self.assertRaisesRegex(SpotifyError,'recover'):
                run(c,root,'me',apply,ids)
            self.assertEqual(len(c.creates),1)

    def test_readback_failure_never_announces_ready(self):
        c=FakeSpotify()
        c.replace=lambda identity,uris: None
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit') as progress:
            with self.assertRaisesRegex(SpotifyError,'Readback'):
                self.start(c,Path(d))
            self.assertFalse(any('Ready to play' in call.args[0] for call in progress.call_args_list))
            self.assertEqual(read_json(Path(d)/'state.json')['status'],'pending')

    def test_changed_source_invalidates_saved_pages(self):
        c=FakeSpotify()
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit'):
            root=Path(d)
            c.fail_page=('0',1)
            with self.assertRaises(SpotifyError):
                self.start(c,root)
            c.fail_page=None
            c.snapshots['0']='v2'
            c.invalidate_playlist=Mock()
            run(c,root,'me',apply,ids)
            c.invalidate_playlist.assert_called_once_with('0')
            self.assertEqual(c.reads.count(('0',0)),2)
            self.assertEqual(c.reads.count(('1',0)),1)
            self.assertEqual(len(c.creates),1)
            self.assertEqual(read_json(root/'setup.json')['imports']['0']['snapshot'],'v2')

    def test_playback_after_ready_moves_into_history(self):
        c=FakeSpotify()
        anchor=dict(track=track('external'),played_at='1',context=None)
        c.events=[anchor]
        def interrupt(message):
            if 'Ready to play' in message:
                raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch('spotify_poc.progressive.emit',side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                self.start(c,root)
            first,second=c.target
            c.events=[anchor,dict(track=track(first),played_at='2',
                                  context={'uri':'spotify:playlist:target'})]
            with patch('spotify_poc.progressive.emit'):
                run(c,root,'me',apply,ids)
            state=read_json(root/'state.json')
            self.assertEqual(state['history'][-1]['id'],first)
            self.assertEqual(c.target[0],second)
            self.assertNotIn(first,c.target)
            self.assertEqual(len(c.creates),1)


class SnapshotDeferralTests(unittest.TestCase):
    def test_target_race_preserves_buffer_and_resume_finishes(self):
        from spotify_poc.client import PlaylistChanged
        from spotify_poc.writes import TargetChanged
        for failure in (PlaylistChanged, TargetChanged):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                client=FakeSpotify()
                root=Path(d)
                original=client.playlist
                armed=[0]
                def playlist(identity):
                    if armed[0]:
                        armed[0]-=1
                        raise failure('changed')
                    return original(identity)
                def progress(message):
                    if 'Ready to play' in message:armed[0]=3
                client.playlist=playlist
                with patch('spotify_poc.progressive.emit',side_effect=progress):
                    run(client,root,'me',apply,ids,sources=list(client.sources),rows=[],minimum=2,normal=6)
                self.assertEqual(read_json(root/'setup.json')['status'],'awaiting_pause')
                self.assertEqual(read_json(root/'state.json')['status'],'ready')
                self.assertEqual(len(client.target),2)
                with patch('spotify_poc.progressive.emit'):
                    run(client,root,'me',apply,ids)
                self.assertEqual(read_json(root/'setup.json')['status'],'complete')
                self.assertEqual(len(client.target),6)
                self.assertEqual(len(client.creates),1)


class AutomaticContinuationTests(unittest.TestCase):
    def test_transient_race_finishes_without_manual_resume(self):
        from spotify_poc.writes import TargetChanged
        client=FakeSpotify();original=client.playlist;armed=[False];retried=[]
        def playlist(identity):
            if armed[0]:
                armed[0]=False;retried.append(True)
                raise TargetChanged('transient change')
            return original(identity)
        def progress(message):
            if 'Ready to play' in message:armed[0]=True
        client.playlist=playlist
        with tempfile.TemporaryDirectory() as d, patch('spotify_poc.progressive.emit',side_effect=progress):
            root=Path(d)
            run(client,root,'me',apply,ids,sources=list(client.sources),rows=[],minimum=2,normal=6)
            self.assertEqual(read_json(root/'setup.json')['status'],'complete')
            self.assertEqual(len(client.target),6)
            self.assertEqual(len(client.creates),1)
            self.assertEqual(len(retried),1)

    def test_complete_source_cache_reused_only_for_matching_snapshot(self):
        from spotify_poc.client import Client
        class CachedFake(FakeSpotify,Client):
            def __init__(self,snapshot):
                FakeSpotify.__init__(self,sources=1,pages=3)
                self.cache=Path('/cache');self.no_store_playlists=set();self.metrics={'source_cache_hits':0}
                self.saved=dict(snapshot=snapshot,rows=[{'item':t} for page in self.sources['0'] for t in page])
            def cache_read(self,path):return self.saved
            def invalidate_playlist(self,identity):self.saved=None
        for snapshot,expected_reads in [('v1',0),('outdated',3)]:
            with self.subTest(snapshot=snapshot),tempfile.TemporaryDirectory() as tmp:
                client=CachedFake(snapshot);ProgressiveTests.start(self,client,Path(tmp))
                self.assertEqual(len(client.reads),expected_reads)
                self.assertEqual(len(client.target),6)
                self.assertEqual(client.metrics['source_cache_hits'],int(snapshot=='v1'))
