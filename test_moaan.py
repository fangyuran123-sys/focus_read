from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server import Desk, make_server


class MoaanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.storage = self.runtime / 'gvfs/mtp:host=Allwinner_EPD105_fixture/内部共享存储空间'
        (self.storage / 'Books/文学').mkdir(parents=True)
        (self.storage / 'Books/Book.epub').write_bytes(b'epub fixture')
        (self.storage / 'Books/文学/Book.txt').write_text('book text')
        (self.storage / 'Android/data').mkdir(parents=True)
        self.desk = Desk(self.root / 'Kindle', self.root / 'data', require_mount=False)
        self.desk.library.metadata_tool = None
        self.moaan = self.desk.moaan
        self.moaan.runtime = self.runtime

    def tearDown(self):
        self.temp.cleanup()

    def test_detection_folder_browsing_offline_cache_and_new_device_isolation(self):
        self.assertTrue(self.moaan.view()['connected'])
        top = self.moaan.scan()
        self.assertEqual([f['name'] for f in top['folders']], ['Books'])
        self.assertFalse(top['reading_supported'])
        listing = self.moaan.scan('Books')
        self.assertEqual([b['format'] for b in listing['books']], ['EPUB'])
        self.assertEqual([f['path'] for f in listing['folders']], ['Books/文学'])
        self.storage.parent.rename(self.storage.parent.with_name('disconnected'))
        offline = self.moaan.view()
        self.assertFalse(offline['connected'])
        self.assertEqual(offline['books'], listing['books'])
        restored = Desk(self.root / 'Kindle', self.desk.data).moaan
        restored.runtime = self.runtime
        self.assertEqual(restored.view()['books'], listing['books'])
        (self.runtime / 'gvfs/mtp:host=Allwinner_EPD105_other/Storage/Books').mkdir(parents=True)
        self.assertTrue(self.moaan.view()['connected'])
        self.assertEqual(self.moaan.view()['books'], [])
        with self.assertRaisesRegex(ValueError, '变化'):
            self.moaan.file(listing['books'][0]['id'])

    def test_no_arbitrary_phone_ambiguous_devices_private_data_or_path_escape(self):
        root = self.moaan.root()
        for path in ['../outside', '/absolute', 'Books//文学', 'Books/../Android', 'Android/data', '.private', 'Books\\file', 4]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.moaan.path(root, path)
        (root / 'Books/escape').symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.moaan.scan('Books/escape')
        (self.runtime / 'gvfs/mtp:host=Other_Phone_123/Storage/Books').mkdir(parents=True)
        self.assertEqual(self.moaan.root(), root)
        (self.runtime / 'gvfs/mtp:host=Allwinner_EPD105_other/Storage/Books').mkdir(parents=True)
        self.assertFalse(self.moaan.view()['connected'])
        with self.assertRaises(ValueError):
            self.moaan.scan()

    def test_import_deduplicates_and_does_not_modify_device_or_kindle_reading_history(self):
        with self.desk.db() as db:
            db.execute('INSERT INTO reading_sessions VALUES (?,?,?)', ('kindle-session', 1700000000000, 1700000060000))
        before = (self.storage / 'Books/Book.epub').read_bytes()
        listing = self.moaan.scan('Books')
        book_id = listing['books'][0]['id']
        with patch('moaan.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, '空间不足'):
                self.moaan.import_book(book_id)
        first = self.moaan.import_book(book_id)
        second = self.moaan.import_book(book_id)
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['book']['id'], second['book']['id'])
        self.assertEqual((self.storage / 'Books/Book.epub').read_bytes(), before)
        self.assertEqual(self.desk.library.listing()['total'], 1)
        self.assertEqual(self.desk.reading_activity(2023)['seconds'], 60)
        (self.storage / 'Books/Book.epub').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.moaan.import_book(book_id)
        self.assertEqual(self.desk.library.listing()['total'], 1)

    def test_send_uses_gio_no_overwrite_and_checks_copy_before_recording(self):
        source = self.root / 'source.epub'
        source.write_bytes(b'original EPUB')
        imported = self.desk.library.import_file(source, 'New.epub')
        fid = imported['file_id']
        state = self.moaan.scan('Books')
        flags = []
        class GioError(Exception):
            pass
        def copy(target, flag, *_args):
            flags.append(flag)
            self.assertIn('/Books/New.epub', target)
            (self.storage / 'Books/New.epub').write_bytes(source.read_bytes())
        fake = SimpleNamespace(
            Gio=SimpleNamespace(FileCopyFlags=SimpleNamespace(NONE=0), File=SimpleNamespace(
                new_for_path=lambda path: SimpleNamespace(copy=copy), new_for_uri=lambda uri: uri)),
            GLib=SimpleNamespace(Error=GioError))
        with patch.dict(sys.modules, {'gi.repository': fake}):
            result = self.moaan.send(fid, 'Books', state['device_id'])
            self.assertIn('SHA-256', result['message'])
            self.assertEqual(flags, [0])
            self.assertEqual(self.desk.library.book(imported['book']['id'])['files'][0]['sent_path'], '墨案 / Books/New.epub')
            with self.assertRaisesRegex(ValueError, '同名'):
                self.moaan.send(fid, 'Books', state['device_id'])
            (self.storage / 'Books/New.epub').unlink()
            def fail(*args):
                raise GioError('exists or disconnected')
            fake.Gio.File.new_for_path = lambda path: SimpleNamespace(copy=fail)
            with self.assertRaisesRegex(ValueError, '传输未完成'):
                self.moaan.send(fid, 'Books', state['device_id'])
            def corrupt(*args):
                (self.storage / 'Books/New.epub').write_bytes(b'wrong bytes')
            fake.Gio.File.new_for_path = lambda path: SimpleNamespace(copy=corrupt)
            with self.assertRaisesRegex(ValueError, '校验失败'):
                self.moaan.send(fid, 'Books', state['device_id'])
        with self.assertRaisesRegex(ValueError, '设备已变化'):
            self.moaan.send(fid, 'Books', 'wrong device')
        with self.assertRaises(ValueError):
            self.moaan.send(fid, '../escape', state['device_id'])
        with self.assertRaises(ValueError):
            self.moaan.send(fid, '', state['device_id'])
        self.assertEqual(source.read_bytes(), b'original EPUB')

    def test_storage_queries_only_accept_valid_capacity(self):
        class GioError(Exception):
            pass
        info = SimpleNamespace(get_attribute_uint64=lambda key: {'filesystem::size': 200, 'filesystem::free': 150}[key])
        fake = SimpleNamespace(Gio=SimpleNamespace(File=SimpleNamespace(new_for_uri=lambda uri: SimpleNamespace(query_filesystem_info=lambda *args: info))),
                               GLib=SimpleNamespace(Error=GioError))
        with patch.dict(sys.modules, {'gi.repository': fake}):
            state = self.moaan.scan('Books')
            self.assertEqual(state['storage'], {'total': 200, 'free': 150, 'used': 50})
            info.get_attribute_uint64 = lambda key: 0
            self.assertIsNone(self.moaan.scan('Books')['storage'])
            def fail(*args):
                raise GioError('not exposed')
            fake.Gio.File.new_for_uri = lambda uri: SimpleNamespace(query_filesystem_info=fail)
            self.assertIsNone(self.moaan.scan('Books')['storage'])

    def test_new_directory_is_no_overwrite_and_does_not_move_any_existing_books(self):
        state = self.moaan.scan('Books')
        before = (self.storage / 'Books/Book.epub').read_bytes()
        for bad in ['../escape', 'bad/name', '.hidden', 'bad?', '', None]:
            with self.subTest(name=bad), self.assertRaises(ValueError):
                self.moaan.create_folder('Books', bad, state['device_id'])
        with self.assertRaisesRegex(ValueError, '同名'):
            self.moaan.create_folder('Books', '文学', state['device_id'])
        with self.assertRaisesRegex(ValueError, '设备已变化'):
            self.moaan.create_folder('Books', '新目录', 'wrong')
        class GioError(Exception):
            pass
        created = []
        def mkdir(uri):
            created.append(uri)
            return SimpleNamespace(make_directory=lambda _cancel: (self.storage / 'Books/New').mkdir())
        fake = SimpleNamespace(Gio=SimpleNamespace(File=SimpleNamespace(new_for_uri=mkdir)), GLib=SimpleNamespace(Error=GioError))
        # Mock the final refresh, not the creation itself.
        with patch.dict(sys.modules, {'gi.repository': fake}), patch.object(self.moaan, 'scan', return_value=state):
            self.moaan.create_folder('Books', 'New', state['device_id'])
        self.assertTrue((self.storage / 'Books/New').is_dir())
        self.assertTrue(created[0].endswith('/Books/New'))
        self.assertEqual((self.storage / 'Books/Book.epub').read_bytes(), before)

    def test_direct_upload_uses_shared_copy_validation_and_records_history_only_on_success(self):
        source = self.root / 'New.txt'; source.write_bytes(b'upload contents')
        state = self.moaan.scan('Books')
        class GioError(Exception):
            pass
        def copy(target, flag, *_):
            self.assertEqual(flag, 0)
            (self.storage / 'Books/New.txt').write_bytes(source.read_bytes())
        fake = SimpleNamespace(Gio=SimpleNamespace(FileCopyFlags=SimpleNamespace(NONE=0), File=SimpleNamespace(
            new_for_uri=lambda uri: uri, new_for_path=lambda path: SimpleNamespace(copy=copy))), GLib=SimpleNamespace(Error=GioError))
        with patch.dict(sys.modules, {'gi.repository': fake}):
            self.moaan.copy_to_device(source, 'New.txt', 'Books', state['device_id'])
            history = self.moaan.view()['transfers']
            self.assertEqual(history[0]['name'], 'New.txt')
            with self.assertRaisesRegex(ValueError, '同名'):
                self.moaan.copy_to_device(source, 'New.txt', 'Books', state['device_id'])
            self.assertEqual(self.moaan.view()['transfers'], history)
            with self.assertRaisesRegex(ValueError, '支持 EPUB'):
                self.moaan.copy_to_device(source, 'Bad.exe', 'Books', state['device_id'])
            source.write_bytes(b'')
            with self.assertRaisesRegex(ValueError, '为空'):
                self.moaan.copy_to_device(source, 'Empty.txt', 'Books', state['device_id'])

    def test_http_origin_token_invalid_payload_and_original_download(self):
        server = make_server(self.desk, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(method, path, body=None, headers=None):
                with closing(http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)) as conn:
                    conn.request(method, path, body=body, headers=headers or {})
                    response = conn.getresponse()
                    return response.status, response.read()
            token = {'X-Desk-Token': self.desk.token}
            self.assertEqual(request('GET', '/api/moaan')[0], 200)
            self.assertEqual(request('POST', '/api/moaan/scan', '{}')[0], 403)
            self.assertEqual(request('GET', '/api/moaan', headers={'Origin': 'https://evil.example'})[0], 403)
            status, raw = request('POST', '/api/moaan/scan', '{"folder":"Books"}', token)
            self.assertEqual(status, 200)
            book_id = json.loads(raw)['books'][0]['id']
            self.assertEqual(request('GET', '/api/moaan/file?id=' + book_id), (200, b'epub fixture'))
            self.assertEqual(request('GET', '/api/moaan/file?id=../../etc/passwd')[0], 409)
            self.assertEqual(request('POST', '/api/moaan/scan', '{"folder":"../"}', token)[0], 400)
            self.assertEqual(request('POST', '/api/moaan/import', '[]', token)[0], 400)
            self.assertEqual(request('POST', '/api/moaan/import', json.dumps({'id': book_id}), token)[0], 200)
            self.assertEqual(request('POST', '/api/moaan/send', '{"id":"bad","folder":"Books","device_id":"wrong"}', token)[0], 400)
            self.assertEqual(request('POST', '/api/moaan/upload?name=Book.txt', b'book')[0], 403)
            self.assertEqual(request('POST', '/api/moaan/folders', '{}')[0], 403)
            self.assertEqual(request('POST', '/api/moaan/folders', '[]', token)[0], 400)
            self.assertEqual(request('POST', '/api/moaan/upload?name=bad.txt&folder=Books&device_id=wrong', b'book', token)[0], 400)
            self.assertEqual(request('POST', '/api/moaan/upload?name=..%2Fbad.txt', b'book', token)[0], 400)
            self.assertEqual(request('GET', '/moaan.js')[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
