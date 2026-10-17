import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spotify_poc.progressive import run
from spotify_poc.__main__ import apply, ids
from spotify_poc.client import SpotifyError, save_json, read_json
from test_progressive import FakeSpotify


class ForceCreateTests(unittest.TestCase):
    def job(self,root):
        old=dict(account='me',sources=['old'],rules=[],minimum=2,normal=6,seed=0,
                 name='Old setup',genres={},imports={},status='importing')
        save_json(root/'setup.json',old)
        (root/'token.json').write_text('existing login')
        return old

    def start(self,c,root,**kwargs):
        run(c,root,'me',apply,ids,sources=['0','1'],rows=[],minimum=2,normal=6,**kwargs)

    def test_force_replaces_unpublished_job_and_keeps_login(self):
        c=FakeSpotify()
        with tempfile.TemporaryDirectory() as d,patch('spotify_poc.progressive.emit'):
            root=Path(d);old=self.job(root)
            self.start(c,root,force=True)
            self.assertEqual(read_json(root/'setup.json')['sources'],['0','1'])
            backups=list(root.glob('setup-backup-*.json'))
            self.assertEqual(len(backups),1)
            self.assertEqual(read_json(backups[0]),old)
            self.assertEqual((root/'token.json').read_text(),'existing login')
            self.assertEqual(len(c.creates),1)

    def test_without_force_preserves_setup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=self.job(root);c=FakeSpotify()
            with self.assertRaisesRegex(SpotifyError,'Existing setup'):
                self.start(c,root)
            self.assertEqual(read_json(root/'setup.json'),old)
            self.assertEqual(c.creates,[])

    def test_force_refuses_existing_or_uncertain_target_state(self):
        for status,target in [('ready','target'),('pending','target'),('creating',None)]:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as d:
                root=Path(d);old=self.job(root);c=FakeSpotify()
                state=dict(status=status,target=target)
                save_json(root/'state.json',state)
                with self.assertRaisesRegex(SpotifyError,'cannot replace'):
                    self.start(c,root,force=True)
                self.assertEqual(read_json(root/'setup.json'),old)
                self.assertEqual(read_json(root/'state.json'),state)
                self.assertFalse(list(root.glob('setup-backup-*.json')))
                self.assertEqual(c.creates,[])

    def test_invalid_replacement_or_account_mismatch_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=self.job(root);c=FakeSpotify()
            for rows,account,minimum in [(None,'me',2),([], 'other',2),([], 'me',7)]:
                with self.subTest(rows=rows,account=account,minimum=minimum),self.assertRaises(SpotifyError):
                    run(c,root,account,apply,ids,sources=['0'],rows=rows,minimum=minimum,normal=6,force=True)
                self.assertEqual(read_json(root/'setup.json'),old)
                self.assertFalse(list(root.glob('setup-backup-*.json')))
