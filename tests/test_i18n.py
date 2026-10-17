import json,re,unittest
from pathlib import Path

class LocalizationTests(unittest.TestCase):
    def test_catalogue_covers_static_and_dynamic_keys(self):
        root=Path(__file__).resolve().parents[1]/'spotify_ui'/'static'
        catalogue=json.loads((root/'en.js').read_text().removeprefix('window.ENGLISH=').rstrip(';\n'))
        html=(root/'index.html').read_text();js=(root/'app.js').read_text()
        keys=re.findall(r'data-i18n(?:-[\w-]+)?="([^"]+)"',html)+re.findall(r'\bt\([\'"]([^\'"]+)',js)
        self.assertTrue(keys)
        self.assertFalse(set(keys)-catalogue.keys())
        for key in ('source_count','selected_sources','songs','next_request','cooldown_seconds'):
            self.assertIn('one',catalogue[key]);self.assertIn('other',catalogue[key])
