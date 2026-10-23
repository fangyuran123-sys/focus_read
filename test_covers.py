from contextlib import closing
from io import BytesIO
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from cover_tools import prepare_cover
from server import Desk, make_server


class CoversTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desk = Desk(self.root / 'device', self.root / 'data', converter='/fake/ebook-convert', require_mount=False)
        self.library = self.desk.library
        self.library.metadata_tool = None
        self.source = self.root / 'original.txt'
        self.source.write_text('原始正文\n' * 20)
        self.book = self.library.import_file(self.source, 'Original.txt')['book']
        self.file_id = self.book['files'][0]['id']
        self.library.metadata_tool = '/fake/ebook-meta'
        self.image = self.root / 'image.png'
        Image.new('RGB', (600, 800), '#346782').save(self.image)

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, args, **kwargs):
        if '--get-cover' in args:
            Path(args[3]).write_bytes(self.embedded)
        else:
            self.assertIn('--cover', args)
            self.embedded = Path(args[args.index('--cover') + 1]).read_bytes()
            output = Path(args[2]) if '--output-profile' in args else Path(args[1])
            output.write_bytes(b'azw3 fixture:' + self.embedded)
        return subprocess.CompletedProcess(args, 0)

    def test_image_validation_and_generated_long_chinese_cover(self):
        output = self.root / 'cover.jpg'
        prepare_cover(output, '这是一本很长的中文书名' * 20, '作者名字' * 40)
        with Image.open(output) as image:
            self.assertEqual((image.format, image.size), ('JPEG', (900, 1200)))
        prepare_cover(output, 'Book', 'Author', self.image)
        with Image.open(output) as image:
            self.assertEqual(image.mode, 'RGB')
            self.assertLessEqual(image.width, 900)
        jpeg = self.root / 'photo.jpg'
        Image.new('RGB', (1200, 800), 'white').save(jpeg)
        prepare_cover(output, 'JPEG', '', jpeg)
        with Image.open(output) as image:
            self.assertEqual(image.size, (900, 600))
        self.image.write_bytes(b'not an image')
        with self.assertRaisesRegex(ValueError, '图片无效'):
            prepare_cover(output, 'Book', '', self.image)
        Image.new('RGB', (4500, 4500)).save(self.image)
        with self.assertRaisesRegex(ValueError, '像素'):
            prepare_cover(output, 'Book', '', self.image)

    def test_replacement_keeps_id_and_repeated_updates_do_not_accumulate_files(self):
        original = self.library.file(self.file_id)[1].read_bytes()
        old = self.root / 'old.azw3'; old.write_bytes(b'old conversion')
        self.library.import_file(old, 'old.azw3', self.book['id'], generated_from=self.file_id)
        with patch('library.subprocess.run', side_effect=self.convert):
            result = self.library.apply_cover(self.file_id, self.image)
            again = self.library.apply_cover(self.file_id, self.image)
        self.assertEqual(result['file_id'], again['file_id'])
        stored = self.library.book(self.book['id'])
        self.assertEqual(len(stored['files']), 2)  # Existing historical version is not silently removed.
        self.assertEqual(len(list(self.library.root.iterdir())), 2)
        self.assertNotEqual(self.library.file(self.file_id)[1].read_bytes(), original)
        source = next(f for f in stored['files'] if f['id'] == self.file_id)
        self.assertIsNone(source['covered_file'])
        self.assertTrue(stored['cover'])
        self.assertTrue(stored['cover_version'])
        self.assertEqual(self.library.cover_path(stored['id']).read_bytes(), self.embedded)
        (self.desk.mount / 'documents').mkdir(parents=True)
        with patch.object(self.desk, 'send', return_value={'message': 'sent'}) as send:
            self.library.send(self.file_id)
            self.assertEqual(send.call_args.args[1], self.library.file(result['file_id'])[0]['name'])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.library.file(source['id'])[0]['format'], 'AZW3')

    def test_send_uses_updated_file_and_epub_send_cache_is_removed(self):
        with patch('library.subprocess.run', side_effect=self.convert):
            first = self.library.apply_cover(self.file_id)
            second = self.library.apply_cover(first['file_id'], self.image)
        (self.desk.mount / 'documents').mkdir(parents=True)
        with patch.object(self.desk, 'send', return_value={'message': 'sent'}) as send:
            self.library.send(self.file_id)
            self.assertEqual(send.call_args.args[1], self.library.file(second['file_id'])[0]['name'])
        self.assertEqual(first['file_id'], second['file_id'])
        self.assertEqual(len(self.library.book(self.book['id'])['files']), 1)
        self.assertEqual(len(list(self.library.root.iterdir())), 1)
        _, azw = self.library.file(self.file_id)
        azw.rename(azw.with_suffix('.epub'))
        with self.desk.db() as db:
            db.execute("UPDATE library_files SET format='EPUB',name='Original.epub' WHERE id=?", (self.file_id,))
        old = self.root / 'cache.azw3'; old.write_bytes(b'cache')
        self.library.import_file(old, 'Original.azw3', self.book['id'], generated_from=self.file_id)
        with patch('library.subprocess.run', side_effect=self.convert):
            updated = self.library.apply_cover(self.file_id, self.image)
        self.assertEqual(updated['book']['files'][0]['format'], 'EPUB')
        self.assertEqual(len(updated['book']['files']), 1)
        self.assertEqual(len(list(self.library.root.iterdir())), 1)

    def test_failures_keep_original_and_previous_cover_and_pointer(self):
        with patch('library.subprocess.run', side_effect=self.convert):
            first = self.library.apply_cover(self.file_id)
        before = self.library.cover_path(self.book['id']).read_bytes()
        with patch('library.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'DRM')):
            with self.assertRaisesRegex(ValueError, '补封面失败'):
                self.library.apply_cover(self.file_id, self.image)
        with patch('library.subprocess.run', side_effect=subprocess.TimeoutExpired('ebook-convert', 180)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.library.apply_cover(self.file_id, self.image)
        def missing_cover(args, **kwargs):
            if '--get-cover' in args:
                Path(args[3]).write_bytes(b'wrong cover')
            else:
                return self.convert(args, **kwargs)
        with patch('library.subprocess.run', side_effect=missing_cover), self.assertRaisesRegex(ValueError, '校验'):
            self.library.apply_cover(self.file_id, self.image)
        with self.desk.db() as db:
            db.execute("CREATE TRIGGER fail_pointer BEFORE UPDATE OF covered_file ON library_files BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with patch('library.subprocess.run', side_effect=self.convert), self.assertRaises(sqlite3.IntegrityError):
            self.library.apply_cover(self.file_id, self.image)
        self.assertEqual(self.library.cover_path(self.book['id']).read_bytes(), before)
        self.assertIsNone(self.library.file(self.file_id)[0]['covered_file'])
        self.assertEqual(len(list(self.library.root.iterdir())), 1)
        self.assertTrue(self.source.exists())

    def test_content_collision_refuses_to_replace_another_book_or_delete_its_file(self):
        with patch('library.subprocess.run', side_effect=self.convert):
            first = self.library.apply_cover(self.file_id, self.image)
        other = self.root / 'other.txt'; other.write_text('another book')
        self.library.metadata_tool = None
        imported = self.library.import_file(other, 'Other.txt')
        self.library.metadata_tool = '/fake/ebook-meta'
        before = self.library.file(imported['file_id'])[1].read_bytes()
        with patch('library.subprocess.run', side_effect=self.convert), self.assertRaisesRegex(ValueError, '相同内容'):
            self.library.apply_cover(imported['file_id'], self.image)
        self.assertEqual(self.library.file(imported['file_id'])[1].read_bytes(), before)
        self.assertTrue(self.library.file(first['file_id'])[1].exists())

    def test_failed_db_commit_preserves_previous_file_and_old_cover(self):
        old_path = self.library.file(self.file_id)[1]
        before = old_path.read_bytes()
        with self.desk.db() as db:
            db.execute("CREATE TRIGGER fail_replacement BEFORE UPDATE OF checksum ON library_files BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with patch('library.subprocess.run', side_effect=self.convert), self.assertRaises(sqlite3.IntegrityError):
            self.library.apply_cover(self.file_id, self.image)
        self.assertEqual(self.library.file(self.file_id)[1], old_path)
        self.assertEqual(old_path.read_bytes(), before)
        self.assertEqual(len(list(self.library.root.iterdir())), 1)
        self.assertFalse(self.library.book(self.book['id'])['cover'])

    def test_cleanup_failure_reports_orphan_but_new_file_is_valid(self):
        old_path = self.library.file(self.file_id)[1]
        original_unlink = Path.unlink
        def fail_old(path, *args, **kwargs):
            if path == old_path:
                raise PermissionError('cleanup denied')
            return original_unlink(path, *args, **kwargs)
        with patch('library.subprocess.run', side_effect=self.convert), patch('library.Path.unlink', fail_old):
            result = self.library.apply_cover(self.file_id, self.image)
        self.assertEqual(result['cleanup_failed'], [old_path.name])
        self.assertTrue(old_path.exists())
        self.assertTrue(self.library.file(self.file_id)[1].exists())
        self.assertEqual(len(result['book']['files']), 1)

    def test_unsupported_format_and_missing_dependencies_are_rejected(self):
        for ext in ['pdf', 'kfx', 'prc']:
            f = self.root / ('file.' + ext); f.write_bytes(ext.encode())
            result = self.library.import_file(f, f.name)
            with self.assertRaisesRegex(ValueError, '暂不支持'):
                self.library.apply_cover(result['file_id'])
        self.library.metadata_tool = None
        with self.assertRaisesRegex(ValueError, 'calibre'):
            self.library.apply_cover(self.file_id)

    def test_http_cover_limits_origin_token_and_upload(self):
        server = make_server(self.desk, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        token = {'X-Desk-Token': self.desk.token}
        try:
            def post(path, body=b'', headers=None):
                with closing(http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)) as conn:
                    conn.request('POST', path, body=body, headers=headers or {})
                    r = conn.getresponse(); return r.status, r.read()
            url = '/api/library/cover?id=' + self.file_id
            self.assertEqual(post(url + '&generate=1')[0], 403)
            self.assertEqual(post(url, b'bad', {**token, 'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(post(url, b'bad', token)[0], 400)
            self.assertEqual(post(url, b'', {**token, 'Content-Length': str(8 * 1024 * 1024 + 1)})[0], 400)
            with patch('library.subprocess.run', side_effect=self.convert):
                status, body = post(url + '&generate=1', headers=token)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)['book']['cover'])
                self.assertEqual(post(url, self.image.read_bytes(), token)[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()


@unittest.skipUnless(shutil.which('ebook-convert') and shutil.which('ebook-meta'), 'calibre required')
class RealCoverTest(unittest.TestCase):
    def test_real_calibre_embeds_cover_into_new_azw3_and_keeps_txt(self):
        with tempfile.TemporaryDirectory(prefix='cover-integration-') as temp:
            root = Path(temp); source = root / 'Book.txt'
            source.write_text('这是正文。\n' * 20)
            desk = Desk(root / 'device', root / 'data', converter=shutil.which('ebook-convert'), require_mount=False)
            result = desk.library.import_file(source, 'Book.txt')
            covered = desk.library.apply_cover(result['file_id'])
            info, path = desk.library.file(covered['file_id'])
            self.assertEqual(info['format'], 'AZW3')
            self.assertEqual(source.read_text(), '这是正文。\n' * 20)
            self.assertTrue(covered['book']['cover'])
            self.assertNotEqual(path, source)
            # Exercise EPUB input too, using calibre to create a real EPUB fixture.
            epub = root / 'Book.epub'
            subprocess.run([desk.converter, str(source), str(epub)], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60, env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
            before = epub.read_bytes()
            imported = desk.library.import_file(epub, 'Book.epub')
            image = root / 'cover.png'; Image.new('RGB', (600, 800), '#556677').save(image)
            updated = desk.library.apply_cover(imported['file_id'], image)
            self.assertEqual(epub.read_bytes(), before)
            self.assertTrue(updated['book']['cover'])
            self.assertEqual(desk.library.file(updated['file_id'])[0]['format'], 'EPUB')
            self.assertEqual(updated['file_id'], imported['file_id'])
            self.assertEqual(len(updated['book']['files']), 1)
            old_path = desk.library.file(updated['file_id'])[1]
            updated_again = desk.library.apply_cover(updated['file_id'])
            self.assertEqual(updated_again['file_id'], updated['file_id'])
            self.assertEqual(len(updated_again['book']['files']), 1)
            new_path = desk.library.file(updated['file_id'])[1]
            if new_path != old_path:
                self.assertFalse(old_path.exists())
            # Metadata-only EPUB modification retains the chapter content exactly.
            import zipfile
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(before)) as original, zipfile.ZipFile(new_path) as changed:
                chapters = [n for n in original.namelist() if n.endswith(('.html', '.xhtml')) and 'titlepage' not in n]
                self.assertTrue(chapters)
                for name in chapters:
                    self.assertEqual(original.read(name), changed.read(name))
            for ext in ('azw3', 'mobi'):
                original_file = root / ('Original.' + ext)
                subprocess.run([desk.converter, str(source), str(original_file)], check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60, env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
                original_bytes = original_file.read_bytes()
                entry = desk.library.import_file(original_file, original_file.name)
                for _ in range(2):
                    changed = desk.library.apply_cover(entry['file_id'])
                    self.assertEqual(changed['file_id'], entry['file_id'])
                    self.assertEqual(len(changed['book']['files']), 1)
                    self.assertEqual(desk.library.file(entry['file_id'])[0]['format'], ext.upper())
                self.assertEqual(original_file.read_bytes(), original_bytes)
