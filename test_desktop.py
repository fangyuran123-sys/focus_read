import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop


class DesktopTest(unittest.TestCase):
    def test_install_quotes_paths_and_does_not_change_source(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_DATA_HOME': temp}):
            with patch.object(desktop, 'BASE', Path('/tmp/books "quoted" 100%')):
                target = desktop.install()
            entry = target.read_text()
            self.assertIn('Type=Application', entry)
            self.assertIn('100%%/desktop.py', entry)
            self.assertIn('\\"quoted\\"', entry)
            self.assertIn('Terminal=false', entry)

    def test_launch_uses_app_window_separate_profile_and_existing_start_script(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_DATA_HOME': temp}), patch('desktop.importlib.util.find_spec', return_value=None):
            with patch('desktop.shutil.which', return_value='/usr/bin/chrome'), patch('desktop.subprocess.run') as run, patch('desktop.subprocess.Popen') as popen:
                desktop.launch()
            self.assertEqual(run.call_args.args[0], ['bash', str(desktop.BASE / 'start.sh')])
            args = popen.call_args.args[0]
            self.assertIn('--app=http://127.0.0.1:8765', args)
            self.assertIn('--user-data-dir=' + temp + '/kindle-desk/browser', args)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_qt_desktop_uses_existing_server_and_no_chrome(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_DATA_HOME': temp}):
            with patch('desktop.importlib.util.find_spec', return_value=True), patch('desktop.subprocess.run') as run, patch('desktop.subprocess.Popen') as popen:
                desktop.launch()
            self.assertEqual(run.call_args.args[0], ['bash', str(desktop.BASE / 'start.sh')])
            self.assertEqual(popen.call_args.args[0][1], str(desktop.BASE / 'desktop_browser.py'))
            self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_missing_browser_does_not_start_service(self):
        with patch('desktop.importlib.util.find_spec', return_value=None), patch('desktop.shutil.which', return_value=None), patch('desktop.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Chrome'):
                desktop.launch()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
