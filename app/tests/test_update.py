"""Check for updates (engine/update.py) against a local HTTPS test server (test_setup.py's). Never shipped.
The check only says what is on the website: these tests also make sure nothing else can be asked of it.

Run:  python3 app/tests/test_update.py
"""
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1])]
import test_setup as T            # noqa: E402
from engine import update         # noqa: E402

ALLOWED = {'127.0.0.1'}


def wait(u, t=20):
    end = time.time() + t
    while time.time() < end:
        s = u.state()
        if s['state'] != 'checking':
            return s
        time.sleep(0.05)
    raise AssertionError(f'still {u.state()}')


class UpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.files = T.Files()
        cls.srv, cls.ctx, cls.base = T.start_server(cls.tmp, cls.files)

    def meta(self, version='1.2.0', min_os='10.0', **extra):
        self.files.data['app-v1.json'] = json.dumps({'version': 1, 'mac': dict(version=version, min_os=min_os, **extra),
                                                     'windows': dict(version=version, min_os=min_os, **extra)}).encode()

    def check(self, version='1.1.2', url=None):
        u = update.Updater(url or f'{self.base}/app-v1.json', allowed=ALLOWED, context=self.ctx, key='mac', version=version)
        u.check()
        return wait(u)

    def test_available(self):
        self.meta(url='https://evil.example/x.dmg', sha256='0' * 64, size=1)    # anything else in the file is not read
        s = self.check()
        self.assertEqual((s['state'], s['version'], s['current'], s['website']), ('available', '1.2.0', '1.1.2', 'lastbook.org'))
        self.assertEqual(set(s), {'state', 'version', 'current', 'website'})

    def test_up_to_date(self):
        self.meta(version='1.1.2')
        self.assertEqual(self.check()['state'], 'up_to_date')

    def test_offline(self):
        s = self.check(url='https://127.0.0.1:1/app-v1.json')     # an allowed address that nothing answers
        self.assertEqual(s['code'], 'offline')
        self.assertIn('virker stadig', s['message'])

    def test_not_published(self):
        self.files.data.pop('app-v1.json', None)
        self.assertEqual(self.check()['state'], 'not_published')

    def test_bad_version(self):
        self.meta(version='<b>2</b>')
        self.assertEqual(self.check()['code'], 'bad_info')

    def test_too_old_os(self):
        self.meta(min_os='99.0')
        self.assertEqual(self.check()['state'], 'too_old_os')

    def test_windows_build_numbers(self):
        self.assertEqual(update._ver('10.0.26200'), (10, 0, 26200))     # five-digit Windows builds must compare
        self.assertGreater(update._ver('10.0.26200'), update._ver('10.0'))

    def test_product_reaches_only_downloads_lastbook_org(self):
        u = update.Updater(key='mac', version='1.0.0')
        self.assertEqual((u.allowed, u.url), ({'downloads.lastbook.org'}, 'https://downloads.lastbook.org/app-v1.json'))
        for other in ('https://pub-28c3cc06eb774c5cac057e6c9343efbd.r2.dev/app-v1.json', 'https://huggingface.co/app-v1.json',
                      'https://downloads.lastbook.org:8443/app-v1.json'):
            u = update.Updater(key='mac', version='1.0.0')
            u.url = other
            u.check()
            s = wait(u)
            self.assertEqual(s['state'], 'error', other)
            self.assertNotEqual(s['state'], 'available')

    def test_only_check_and_state(self):
        self.assertEqual(set(update.ROUTES), {('GET', 'state'), ('POST', 'check')})
        for gone in ('download', 'install', 'stop', '_install_mac', '_install_windows'):
            self.assertFalse(hasattr(update.Updater, gone), gone)
        self.assertFalse(hasattr(update, 'QUIT'))
        self.assertNotIn('subprocess', vars(update))      # nothing to start: no program is run


if __name__ == '__main__':
    unittest.main(verbosity=2)
