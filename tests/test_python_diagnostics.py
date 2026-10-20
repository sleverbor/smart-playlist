import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from spotify_app.client import Client,RateLimited,SpotifyError,save_json,read_json
from test_rate_limits import Clock

class DiagnosticsTests(unittest.TestCase):
    def test_shared_429_evidence_and_all_endpoint_results_survive_new_client(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);token=root/'token.json';save_json(token,dict(client_id='app',access_token='SECRET',expires_at=9999999999))
            clock=Clock();client=Client(token,interval=0,retries=0,clock=clock.time,sleep=clock.sleep)
            with patch('spotify_app.client.http_json',side_effect=RateLimited('77880')):
                with self.assertRaises(SpotifyError):client.request('GET','/playlists/private-id/items?offset=50')
            state=read_json(root/'rate.json');diag=state['diagnostics'];self.assertEqual(diag['last_429']['retry_after'],'77880');self.assertEqual(diag['last_429']['retry_until'],clock.time()+77880)
            self.assertEqual(diag['recent'][0]['endpoint'],'/playlists/:id/items');self.assertNotIn('SECRET',json.dumps(diag));self.assertNotIn('private-id',json.dumps(diag))
            restarted=Client(token,interval=0,retries=0,clock=clock.time,sleep=clock.sleep)
            with patch('spotify_app.client.http_json',return_value=({}, {},200)) as send:restarted.request('GET','/me')
            self.assertEqual(send.call_count,1);self.assertGreaterEqual(clock.time(),state['retry_until'])
            self.assertEqual(read_json(root/'rate.json')['diagnostics']['by_endpoint']['GET /me'],1)

    def test_diagnostic_window_is_bounded_without_losing_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            token=Path(folder)/'token.json';save_json(token,dict(client_id='app',access_token='fake',expires_at=9999999999));clock=Clock();client=Client(token,interval=0,clock=clock.time,sleep=clock.sleep)
            for i in range(140):client.gate.record('GET','/v1/playlists/'+str(i),200,.01)
            diag=read_json(Path(folder)/'rate.json')['diagnostics'];self.assertEqual(diag['total_requests'],140);self.assertEqual(len(diag['recent']),100);self.assertEqual(diag['by_endpoint'],{'GET /playlists/:id':140})
