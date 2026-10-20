import json,unittest
from pathlib import Path
from spotify_ui.errors import describe,MESSAGES
from spotify_app.client import SpotifyError

class ServiceErrorTests(unittest.TestCase):
    def test_codes_do_not_depend_on_http_diagnostic_wording(self):
        for status,code in ((401,'authentication'),(403,'permission'),(404,'unavailable'),(429,'rate_limit')):
            self.assertEqual(describe(SpotifyError('technical detail',status))['code'],code)
        self.assertEqual(describe(RuntimeError('/private/path traceback')) ,dict(code='unexpected',params={}))
    def test_cooldown_has_numeric_translation_parameters(self):
        error=SpotifyError('diagnostic');error.ui_code='cooldown';error.ui_params={'deadline':12345}
        self.assertEqual(describe(error),dict(code='cooldown',params={'deadline':12345}))
    def test_every_product_error_has_english_translation(self):
        source=Path(__file__).resolve().parents[1]/'spotify_ui/static/en.js'
        catalogue=json.loads(source.read_text().removeprefix('window.ENGLISH=').rstrip(';\n'))
        for code in MESSAGES:self.assertEqual(catalogue['error_'+code],MESSAGES[code])
