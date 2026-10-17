import tempfile
import unittest
from pathlib import Path
from spotify_poc.__main__ import load_rules
from spotify_poc.sync import rules_from
from spotify_poc.client import SpotifyError

class RulesLoadingTests(unittest.TestCase):
    def test_default_rules(self):
        self.assertEqual(len(rules_from(load_rules(None), [])), 3)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(SpotifyError, 'Cannot load rules file'):
                load_rules(str(Path(d) / 'missing.json'))

    def test_invalid_documents(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rules.json'
            for text in ('null', '{}', '[null]', 'broken'):
                with self.subTest(text=text):
                    p.write_text(text)
                    with self.assertRaises(SpotifyError):
                        load_rules(str(p))

    def test_valid_empty_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rules.json'
            p.write_text('[]')
            self.assertEqual(load_rules(str(p)), [])

    def test_engine_boundary_rejects_null(self):
        with self.assertRaisesRegex(SpotifyError, 'list of rule objects'):
            rules_from(None, [])
