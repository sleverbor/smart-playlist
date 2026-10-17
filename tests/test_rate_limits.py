import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from spotify_poc.client import Client, SpotifyError, RateLimited, http_json, save_json


class Clock:
    def __init__(self):
        self.now=1000.0
        self.waits=[]
    def time(self):
        return self.now
    def sleep(self,seconds):
        self.waits.append(seconds)
        self.now += seconds


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=Clock()
        self.token=self.root/'token.json'
        save_json(self.token,dict(client_id='app',access_token='secret',refresh_token='refresh',expires_at=9999999999))
        self.client=Client(self.token,interval=1,clock=self.clock.time,sleep=self.clock.sleep)

    def test_paces_requests_and_shares_cooldown_across_clients(self):
        other=Client(self.token,interval=1,clock=self.clock.time,sleep=self.clock.sleep)
        with patch('spotify_poc.client.http_json',return_value=({}, {}, 200)):
            self.client.request('GET','/me')
            other.request('GET','/me')
            self.client.gate.defer(7)
            other.request('GET','/me')
        self.assertEqual(self.clock.waits,[1,7])

    def test_honors_retry_after_and_retries_same_batch(self):
        with patch('spotify_poc.client.http_json',side_effect=[RateLimited('4'),({}, {}, 200)]) as transport:
            self.client.request('POST','/me/playlists',{'name':'mix'})
        self.assertEqual(transport.call_count,2)
        self.assertEqual(transport.call_args_list[0],transport.call_args_list[1])
        self.assertEqual(self.clock.waits,[4])
        self.assertEqual(self.client.metrics['rate_limits'],1)

    def test_repeated_429_is_bounded_and_persists_final_cooldown(self):
        with patch('spotify_poc.client.http_json',side_effect=RateLimited('2')) as transport:
            with self.assertRaisesRegex(SpotifyError,'cooldown saved'):
                self.client.request('GET','/me')
        self.assertEqual(transport.call_count,4)
        self.assertEqual(self.clock.waits,[2,2,2])
        self.assertEqual(json.loads((self.root/'rate.json').read_text())['next'],self.clock.now+2)

    def test_missing_invalid_retry_after_uses_backoff(self):
        for header in (None,'bad','NaN','-3'):
            with self.subTest(header=header):
                self.clock.now+=100
                self.clock.waits=[]
                with patch('spotify_poc.client.http_json',side_effect=[RateLimited(header),RateLimited(header),({}, {},200)]):
                    self.client.request('GET','/me')
                self.assertEqual(self.clock.waits,[2,4])

    def test_long_retry_after_stops_without_retrying_early(self):
        with patch('spotify_poc.client.http_json',side_effect=RateLimited('500')) as transport:
            with self.assertRaises(SpotifyError):
                self.client.request('GET','/me')
        self.assertEqual(transport.call_count,1)
        self.assertEqual(self.clock.waits,[])
        self.assertEqual(json.loads((self.root/'rate.json').read_text())['next'],1500)

    def test_ambiguous_mutation_failure_is_never_retried(self):
        for method,path in [('POST','/me/playlists'),('PUT','/playlists/x/items'),('POST','/playlists/x/items')]:
            with self.subTest(method=method,path=path):
                with patch('spotify_poc.client.urlopen',side_effect=URLError('offline')) as opener:
                    with self.assertRaisesRegex(SpotifyError,'connection was interrupted'):
                        self.client.request(method,path,{})
                self.assertEqual(opener.call_count,1)

    def test_interrupted_backoff_retains_cooldown(self):
        self.client.gate.sleep=Mock(side_effect=KeyboardInterrupt)
        with patch('spotify_poc.client.http_json',side_effect=RateLimited('4')) as transport:
            with self.assertRaises(KeyboardInterrupt):
                self.client.request('GET','/me')
        self.assertEqual(transport.call_count,1)
        self.assertEqual(json.loads((self.root/'rate.json').read_text())['next'],1004)

    def test_http_429_and_304_preserve_status_and_headers(self):
        headers=Message();headers['Retry-After']='9';headers['ETag']='v1'
        with patch('spotify_poc.client.urlopen',side_effect=HTTPError('url',429,'rate',headers,None)):
            with self.assertRaises(RateLimited) as caught:
                http_json('GET','https://example.com')
            self.assertEqual(caught.exception.retry_after,'9')
        with patch('spotify_poc.client.urlopen',side_effect=HTTPError('url',304,'same',headers,None)):
            self.assertEqual(http_json('GET','https://example.com',details=True),(None,headers,304))

    def test_etag_persists_and_304_reuses_body(self):
        with patch('spotify_poc.client.http_json',return_value=({'id':'a'}, {'ETag':'v1'}, 200)):
            self.client.request('GET','/tracks/a')
        other=Client(self.token,interval=1,clock=self.clock.time,sleep=self.clock.sleep)
        with patch('spotify_poc.client.http_json',return_value=(None,{},304)) as transport:
            self.assertEqual(other.request('GET','/tracks/a'),{'id':'a'})
        self.assertEqual(transport.call_args.kwargs['extra_headers'],{'If-None-Match':'v1'})
        self.assertEqual(other.metrics['cache_hits'],1)

    def test_changed_etag_replaces_body_and_no_store_discards_cache(self):
        responses=[({'v':1},{'ETag':'v1'},200),({'v':2},{'ETag':'v2'},200),({'v':3},{'ETag':'v3','Cache-Control':'no-store'},200)]
        with patch('spotify_poc.client.http_json',side_effect=responses):
            for i in range(1,4):
                self.assertEqual(self.client.request('GET','/tracks/a'),{'v':i})
        self.assertEqual(list(self.client.cache.rglob('*.json')),[])

    def test_fresh_cache_avoids_request_then_expires(self):
        with patch('spotify_poc.client.http_json',return_value=({'v':1},{'ETag':'v1','Cache-Control':'max-age=10'},200)) as transport:
            self.client.request('GET','/tracks/a')
            self.client.request('GET','/tracks/a')
            self.assertEqual(transport.call_count,1)
            self.clock.now+=11
            self.client.request('GET','/tracks/a')
            self.assertEqual(transport.call_count,2)

    def test_critical_reads_ignore_cache_and_writes_invalidate_pages(self):
        with patch('spotify_poc.client.http_json',return_value=({}, {'ETag':'v1','Cache-Control':'max-age=30'},200)) as transport:
            for path in ('/me','/me/player/recently-played','/playlists/x'):
                self.client.request('GET',path)
                self.client.request('GET',path)
            self.assertEqual(transport.call_count,6)
            self.client.request('GET','/playlists/x/items?limit=50')
            self.assertTrue(list(self.client.cache.rglob('*.json')))
            self.client.request('PUT','/playlists/x/items',{'uris':[]})
            self.assertFalse(list(self.client.cache.rglob('*.json')))

    def test_cache_is_isolated_from_other_credentials(self):
        with patch('spotify_poc.client.http_json',return_value=({}, {'ETag':'v1'},200)):
            self.client.request('GET','/tracks/a')
        save_json(self.token,dict(client_id='app',access_token='other',refresh_token='different',expires_at=9999999999))
        other=Client(self.token,interval=1,clock=self.clock.time,sleep=self.clock.sleep)
        with patch('spotify_poc.client.http_json',return_value=({}, {},200)) as transport:
            other.request('GET','/tracks/a')
        self.assertEqual(transport.call_args.kwargs['extra_headers'],{})

    def test_unexpected_304_without_cache_stops(self):
        with patch('spotify_poc.client.http_json',return_value=(None,{},304)):
            with self.assertRaisesRegex(SpotifyError,'without cached'):
                self.client.request('GET','/tracks/a')

    def test_source_snapshot_cache_avoids_pages_but_changed_snapshot_refetches(self):
        metadata={'snapshot_id':'v1'}
        rows=[{'item':{'id':'a'}}]
        with patch.object(self.client,'request',return_value=metadata), patch.object(self.client,'playlist',return_value=(metadata,rows)) as download:
            self.assertEqual(self.client.source_playlist('x')[1],rows)
            self.assertEqual(self.client.source_playlist('x')[1],rows)
            self.assertEqual(download.call_count,1)
        with patch.object(self.client,'request',return_value={'snapshot_id':'v2'}), patch.object(self.client,'playlist',return_value=({'snapshot_id':'v2'},[])) as download:
            self.assertEqual(self.client.source_playlist('x')[1],[])
            self.assertEqual(download.call_count,1)

    def test_deleted_source_does_not_fall_back_to_cache(self):
        with patch.object(self.client,'request',return_value={'snapshot_id':'v1'}), patch.object(self.client,'playlist',return_value=({'snapshot_id':'v1'},[])):
            self.client.source_playlist('x')
        with patch.object(self.client,'request',side_effect=SpotifyError('HTTP 404')):
            with self.assertRaisesRegex(SpotifyError,'404'):
                self.client.source_playlist('x')

    def test_partial_download_is_not_saved_as_complete_source(self):
        with patch.object(self.client,'request',return_value={'snapshot_id':'v1'}), patch.object(self.client,'playlist',side_effect=SpotifyError('offline')):
            with self.assertRaises(SpotifyError):
                self.client.source_playlist('x')
        self.assertFalse((self.client.cache/'source-x.json').exists())

    def test_multi_batch_retry_does_not_replay_successful_batches(self):
        with patch('spotify_poc.client.http_json',side_effect=[({}, {},200),RateLimited('2'),({}, {},200),({}, {},200)]) as transport:
            self.client.replace('x',['uri']*205)
        batches=[len(call.args[2]['uris']) for call in transport.call_args_list]
        self.assertEqual(batches,[100,100,100,5])

    def test_maximum_source_cached_sync_uses_one_call_instead_of_pages(self):
        def response(method,url,*args,**kwargs):
            if '/items' not in url:
                return {'snapshot_id':'v1'}, {},200
            page=int(url.split('page=')[-1]) if 'page=' in url else 0
            return dict(items=[{'item':{'id':str(i)}} for i in range(page*50,(page+1)*50)],
                        next=f'/playlists/x/items?page={page+1}' if page<199 else None), {},200
        with patch('spotify_poc.client.http_json',side_effect=response) as transport:
            self.assertEqual(len(self.client.source_playlist('x')[1]),10000)
            self.assertEqual(transport.call_count,203)
            transport.reset_mock()
            self.assertEqual(len(self.client.source_playlist('x')[1]),10000)
            self.assertEqual(transport.call_count,1)

    def test_no_store_playlist_does_not_save_complete_source_cache(self):
        def response(method,url,*args,**kwargs):
            if '/items' not in url:
                return {'snapshot_id':'v1'}, {},200
            return {'items':[], 'next':None}, {'Cache-Control':'no-store'},200
        with patch('spotify_poc.client.http_json',side_effect=response):
            self.client.source_playlist('x')
        self.assertFalse((self.client.cache/'source-x.json').exists())

    def test_target_readback_refetches_pages_even_if_etag_cache_is_fresh(self):
        def response(method,url,*args,**kwargs):
            body={'snapshot_id':'v1'} if '/items' not in url else {'items':[], 'next':None}
            return body, {'ETag':'v1','Cache-Control':'max-age=3600'},200
        with patch('spotify_poc.client.http_json',side_effect=response) as transport:
            self.client.playlist('x')
            self.client.playlist('x')
        self.assertEqual(transport.call_count,6)

    def test_partial_write_leaves_pending_queue_for_recovery(self):
        from spotify_poc.__main__ import apply
        state=dict(account='me',target='x',status='pending',queue=[{'id':str(i)} for i in range(205)])
        path=self.root/'state.json'
        save_json(path,state)
        remote=[]
        def response(method,url,data=None,*args,**kwargs):
            if method=='PUT':
                remote[:]=[u.split(':')[-1] for u in data['uris']]
                return {},{},200
            if method=='POST':
                raise SpotifyError('Network failure; write outcome may be unknown')
            if '/items' in url:
                return {'items':[{'item':{'id':i}} for i in remote], 'next':None},{},200
            return {'snapshot_id':'v1','owner':{'id':'me'}},{},200
        with patch('spotify_poc.client.http_json',side_effect=response) as transport:
            with self.assertRaises(SpotifyError):
                apply(self.client,path,state)
        self.assertEqual(json.loads(path.read_text()),state)
        self.assertEqual(state['status'],'pending')
        writes=[call.args[0] for call in transport.call_args_list if call.args[0]!='GET']
        self.assertEqual(writes,['PUT','POST'])

    def test_default_policy_paces_reads_and_writes_across_clients(self):
        a=Client(self.token,clock=self.clock.time,sleep=self.clock.sleep)
        b=Client(self.token,clock=self.clock.time,sleep=self.clock.sleep)
        with patch('spotify_poc.client.http_json',return_value=({}, {},200)):
            a.request('GET','/me')
            b.request('POST','/me/playlists',{})
            a.request('POST','/playlists/x/items',{})
        self.assertEqual(self.clock.waits,[6,6])

    def test_adaptive_slowdown_survives_restart_and_cooldown_is_not_shortened(self):
        from spotify_poc.client import RateGate
        gate=RateGate(self.root/'policy.json',3,self.clock.time,self.clock.sleep,{'wait_seconds':0},conservative=True)
        gate.defer(500);gate.defer(2)
        saved=json.loads(gate.path.read_text())
        self.assertEqual(saved['next'],1500);self.assertEqual(saved['multiplier'],4)
        other=RateGate(gate.path,3,self.clock.time,self.clock.sleep,{'wait_seconds':0},conservative=True)
        other.acquire();self.assertEqual(json.loads(gate.path.read_text())['next'],1512)

    def test_rolling_budget_applies_even_after_legacy_fast_reservations(self):
        from spotify_poc.client import RateGate
        path=self.root/'policy.json';save_json(path,dict(next=0,recent=[999]*10))
        gate=RateGate(path,3,self.clock.time,self.clock.sleep,{'wait_seconds':0},conservative=True)
        gate.acquire()
        self.assertAlmostEqual(sum(self.clock.waits),29.01)

    def test_oauth_429_saves_cooldown_without_replaying_token_exchange(self):
        from spotify_poc.client import token_request
        with patch('spotify_poc.client.time.time',self.clock.time),patch('spotify_poc.client.time.sleep',self.clock.sleep),patch('spotify_poc.client.http_json',side_effect=RateLimited('500')) as transport:
            with self.assertRaisesRegex(SpotifyError,'login requests'):
                token_request(self.token,'app',{'grant_type':'refresh_token'})
        self.assertEqual(transport.call_count,1)
        path=next(self.root.glob('oauth-rate-*.json'))
        self.assertEqual(json.loads(path.read_text())['next'],1500)
