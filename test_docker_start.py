import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DockerStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'app with spaces'
        self.app.mkdir()
        shutil.copy(Path(__file__).with_name('docker-start.sh'), self.app)
        self.device = self.root / 'Kindle device'
        (self.device / 'documents').mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'DOCKER_CALLS': str(self.root / 'calls'), 'NATIVE_ACTIVE': '1',
                    'MOUNT_OK': '0', 'IMAGE_OK': '0'}
        for name in ['KINDLE_IMAGE', 'KINDLE_DATA', 'LOCAL_UID', 'LOCAL_GID']:
            self.env.pop(name, None)
        self.mock('mountpoint', 'exit "$MOUNT_OK"')
        self.mock('systemctl', 'exit "$NATIVE_ACTIVE"')
        self.mock('docker', '''printf '%s\\n' "$*" >> "$DOCKER_CALLS"
if [[ "$1 $2" == 'image inspect' ]]; then exit "$IMAGE_OK"; fi
if [[ "$*" == *' up '* ]]; then
    printf '%s\\n' "$KINDLE_MOUNT" "$KINDLE_DATA" "$KINDLE_IMAGE" "$LOCAL_UID:$LOCAL_GID" > "$DOCKER_CALLS.env"
fi''')

    def mock(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def start(self, *args):
        return subprocess.run(['bash', str(self.app / 'docker-start.sh'), *map(str, args)],
                              cwd=self.root, env=self.env, text=True, capture_output=True)

    def test_argument_and_unmounted_device_rejected_before_docker(self):
        self.assertEqual(self.start().returncode, 2)
        self.env['MOUNT_OK'] = '1'
        result = self.start(self.device)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('尚未挂载', result.stderr)
        self.assertFalse((self.root / 'calls').exists())

    def test_native_service_blocks_start_without_stopping_it(self):
        self.env['NATIVE_ACTIVE'] = '0'
        result = self.start(self.device)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('本机版仍在运行', result.stderr)
        self.assertFalse((self.root / 'calls').exists())

    def test_missing_image_does_not_start_or_pull(self):
        self.env['IMAGE_OK'] = '1'
        result = self.start(self.device)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('docker load', result.stderr)
        calls = (self.root / 'calls').read_text()
        self.assertNotIn(' up ', calls)
        self.assertNotIn('pull', calls)

    def test_start_recreates_with_current_user_and_ignores_old_env(self):
        (self.app / '.env').write_text('KINDLE_IMAGE=old-image\ntouch "$DOCKER_CALLS.injected"\n')
        result = self.start(self.device)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / 'data').is_dir())
        calls = (self.root / 'calls').read_text()
        self.assertIn('--env-file /dev/null', calls)
        self.assertIn('--no-build --force-recreate --wait', calls)
        self.assertFalse((self.root / 'calls.injected').exists())
        self.assertEqual((self.root / 'calls.env').read_text().splitlines(),
                         [str(self.device), str(self.app / 'data'), 'kindle-desk:0.2.0', f'{os.getuid()}:{os.getgid()}'])

    def test_explicit_relative_data_directory_uses_script_location(self):
        self.env.update(KINDLE_DATA='custom data', KINDLE_IMAGE='kindle-desk:custom')
        result = self.start(self.device)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / 'custom data').is_dir())
        self.assertIn('image inspect kindle-desk:custom', (self.root / 'calls').read_text())


if __name__ == '__main__':
    unittest.main()
