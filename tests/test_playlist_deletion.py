"""Unavailable-target failures, simulated without Spotify credentials."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from spotify_app.__main__ import apply, ids, main
from spotify_app.client import SpotifyError
from spotify_app.progressive import run
import test_progressive
from test_progressive import FakeSpotify


class DeletedTargetSpotify(FakeSpotify):
    def __init__(self):
        super().__init__()
        self.deleted = False
        self.replace = Mock(wraps=self.replace)

    def request(self, method, path, data=None):
        if method == 'GET' and path == '/me':
            return {'id': 'me'}
        if self.deleted and path.startswith('/playlists/target'):
            raise SpotifyError('Spotify HTTP 404; target unavailable')
        return super().request(method, path, data)


class PlaylistDeletionTests(unittest.TestCase):
    def test_deleted_during_progressive_setup_preserves_published_state(self):
        client = DeletedTargetSpotify()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = []
            def delete_after_ready(message):
                if 'Ready to play' in message:
                    saved.append((root / 'state.json').read_bytes())
                    client.deleted = True
            with patch('spotify_app.progressive.emit', side_effect=delete_after_ready):
                with self.assertRaisesRegex(SpotifyError, '404'):
                    test_progressive.ProgressiveTests().start(client, root)
            self.assertEqual((root / 'state.json').read_bytes(), saved[0])
            self.assertEqual(len(client.creates), 1)
            self.assertEqual(client.replace.call_count, 1)

    def paused_setup(self, client, root):
        def pause(message):
            if 'Ready to play' in message:
                raise KeyboardInterrupt()
        with patch('spotify_app.progressive.emit', side_effect=pause):
            with self.assertRaises(KeyboardInterrupt):
                test_progressive.ProgressiveTests().start(client, root)
        client.deleted = True
        client.replace.reset_mock()
        return (root / 'state.json').read_bytes()

    def test_deleted_before_resume_does_not_recreate_or_write(self):
        client = DeletedTargetSpotify()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = self.paused_setup(client, root)
            with patch('spotify_app.progressive.emit'), self.assertRaisesRegex(SpotifyError, '404'):
                run(client, root, 'me', apply, ids)
            self.assertEqual((root / 'state.json').read_bytes(), saved)
            self.assertEqual(len(client.creates), 1)
            client.replace.assert_not_called()

    def test_deleted_before_sync_preview_and_apply_preserves_state(self):
        for flags in ([], ['--apply']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as folder:
                client = DeletedTargetSpotify()
                root = Path(folder)
                saved = self.paused_setup(client, root)
                with patch('spotify_app.__main__.Client', return_value=client), \
                     patch('spotify_app.__main__.library', return_value=([], {}, {})), \
                     patch('sys.argv', ['spotify_app', '--accounts-directory', str(Path(folder)/'accounts'), '--storage', 'json', '--directory', folder, 'sync'] + flags), \
                     self.assertRaisesRegex(SpotifyError, '404'):
                    main()
                self.assertEqual((root / 'state.json').read_bytes(), saved)
                self.assertEqual(len(client.creates), 1)
                client.replace.assert_not_called()
