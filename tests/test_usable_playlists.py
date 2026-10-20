import unittest
from unittest.mock import Mock
from spotify_app.__main__ import usable_playlists
from spotify_app.client import SpotifyError


def playlist(identity, owner, collaborative=False):
    return dict(id=identity,name=identity,owner={'id':owner},collaborative=collaborative)


class UsablePlaylistTests(unittest.TestCase):
    def test_owned_and_confirmed_collaborative_only(self):
        client=Mock()
        client.pages.return_value=[playlist('own','me'),playlist('followed','other'),playlist('collab','other',True)]
        rows=list(usable_playlists(client,'me'))
        self.assertEqual([(row['id'],access) for row,access in rows],[('own','owned'),('collab','collaborative')])
        self.assertEqual([call.args[1] for call in client.request.call_args_list],
                         ['/playlists/own/items?limit=1','/playlists/collab/items?limit=1'])

    def test_unavailable_owned_and_unconfirmed_collaborative_are_excluded(self):
        client=Mock()
        client.pages.return_value=[playlist('deleted','me'),playlist('forbidden','other',True)]
        client.request.side_effect=[SpotifyError('missing',404),SpotifyError('forbidden',403)]
        self.assertEqual(list(usable_playlists(client,'me')),[])

    def test_network_and_rate_limit_errors_are_not_hidden(self):
        for status in (None,429,500):
            with self.subTest(status=status):
                client=Mock();client.pages.return_value=[playlist('own','me')]
                client.request.side_effect=SpotifyError('request failed',status)
                with self.assertRaises(SpotifyError):
                    list(usable_playlists(client,'me'))
