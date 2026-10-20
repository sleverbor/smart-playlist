import unittest
from unittest.mock import Mock
from spotify_app.client import Client, PlaylistChanged

class SetupReadTests(unittest.TestCase):
    def client(self, snapshots):
        client=Client.__new__(Client)
        client.request=Mock(side_effect=[{'snapshot_id':s} for s in snapshots])
        client.pages=Mock(return_value=[{'item':{'id':'song'}}])
        return client

    def test_changed_download_restarts_and_returns_only_stable_read(self):
        client=self.client(['a','b','b','b'])
        metadata,rows=client.playlist('target')
        self.assertEqual(metadata['snapshot_id'],'b')
        self.assertEqual(len(rows),1)
        self.assertEqual(client.pages.call_count,2)

    def test_continuously_changing_download_has_bounded_retries(self):
        client=self.client(['a','b','c','d','e','f'])
        with self.assertRaises(PlaylistChanged):client.playlist('target')
        self.assertEqual(client.pages.call_count,3)
