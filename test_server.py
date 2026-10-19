import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from server import Desk, make_server, markdown_zip, parse_clippings

CLIPPINGS = '\ufeff读书与生活 (作者)\n- 您在位置 #12-14的标注 | 添加于 2026年10月19日星期一 上午10:00:00\n\n这是第一段。\n这是第二行。\n==========\nReading (Author)\n- Your Note on Location 22 | Added on Monday, October 19, 2026 10:01:00 AM\n\nA note with <script>literal markup</script>.\n==========\n'


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mount = self.root / 'device'
        (self.mount / 'documents').mkdir(parents=True)
        self.desk = Desk(self.mount, self.root / 'data', require_mount=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_clippings_multiline_types_and_idempotence(self):
        notes, skipped = parse_clippings(CLIPPINGS)
        self.assertEqual(skipped, 0)
        self.assertEqual([n['kind'] for n in notes], ['highlight', 'note'])
        self.assertEqual(notes[0]['text'], '这是第一段。\n这是第二行。')
        self.assertEqual(self.desk.import_notes(notes), 2)
        self.assertEqual(self.desk.import_notes(notes), 0)
        self.assertEqual(len(self.desk.view()['notes']), 2)
        with zipfile.ZipFile(io.BytesIO(markdown_zip(notes))) as z:
            self.assertEqual(len(z.namelist()), 2)
            self.assertTrue(all('/' not in n for n in z.namelist()))
            self.assertIn('> 这是第一段。', z.read(z.namelist()[0]).decode())

    def test_malformed_clippings_are_not_silently_accepted(self):
        self.assertEqual(parse_clippings('not a clipping'), ([], 1))
        self.assertEqual(parse_clippings('Title\n- Your Highlight | Date\n\n=========='), ([], 1))

    def test_scan_skips_sidecars_symlinks_and_preserves_offline_books(self):
        docs = self.mount / 'documents'
        (docs / '书_AABBCCDDEEFF.kfx').write_bytes(b'book')
        (docs / 'My Clippings.txt').write_text(CLIPPINGS)
        (docs / 'book.sdr').mkdir()
        (docs / 'book.sdr' / 'not-a-book.txt').write_text('metadata')
        (docs / 'escape.pdf').symlink_to('/etc/passwd')
        result = self.desk.scan()
        self.assertEqual(len(result['books']), 1)
        self.assertEqual(result['books'][0]['title'], '书')
        self.assertEqual(len(result['notes']), 2)
        self.mount.rename(self.root / 'disconnected')
        result = self.desk.scan()
        self.assertFalse(result['connected'])
        self.assertEqual(len(result['books']), 1)
        self.assertEqual(len(result['notes']), 2)

    def test_transfer_no_overwrite_or_path_escape(self):
        source = self.root / 'source.txt'
        source.write_text('original')
        self.desk.send(source, 'book.txt')
        target = self.mount / 'documents' / 'Kindle Desk' / 'book.txt'
        self.assertEqual(target.read_text(), 'original')
        source.write_text('replacement')
        with self.assertRaisesRegex(ValueError, '同名'):
            self.desk.send(source, 'book.txt')
        self.assertEqual(target.read_text(), 'original')
        for name in ['../escape.txt', '/escape.txt', '..\\escape.txt', 'nul\x00.txt']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.desk.send(source, name)

    def test_symlink_upload_folder_and_disconnection_rejected(self):
        source = self.root / 'source.txt'
        source.write_text('content')
        (self.mount / 'documents' / 'Kindle Desk').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.desk.send(source, 'escape.txt')
        self.mount.rename(self.root / 'offline')
        with self.assertRaisesRegex(ValueError, '断开'):
            self.desk.send(source, 'book.txt')
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_database_notes_copied_read_only_and_positions_not_invented(self):
        folder = self.mount / 'system/ksdk/.annotations/account'
        folder.mkdir(parents=True)
        path = folder / 'ksdk_annotation_v1.db'
        db = sqlite3.connect(path)
        for name, key in [('server_view', 'dataset_id'), ('local_edit', 'dataset_id'), ('nonsyncable_annotations', 'book_id')]:
            db.execute('CREATE TABLE '+name+'(annotation_id TEXT,'+key+' TEXT,dataset INTEGER,serialized_payload TEXT)')
        note = json.dumps({'start_position': {'shortPosition': 15}, 'json_metadata': json.dumps({'note_text':'真实笔记'})})
        for table in ['server_view', 'local_edit']:
            db.execute('INSERT INTO '+table+' VALUES (?,?,?,?)', ('n1','BOOK-1',3,note))
        db.execute('INSERT INTO nonsyncable_annotations VALUES (?,?,?,?)', ('h1','BOOK',1,json.dumps({'start_position':{'shortPosition':20}})))
        db.commit(); db.close()
        before = path.read_bytes()
        result = self.desk.scan()
        self.assertEqual(len(result['notes']), 2)
        self.assertEqual(path.read_bytes(), before)
        highlight = next(n for n in result['notes'] if n['kind']=='highlight')
        self.assertTrue(highlight['pending'])
        self.assertEqual(highlight['location'], '内部位置 20')
        self.assertEqual(next(n for n in result['notes'] if n['kind']=='note')['text'], '真实笔记')

    def test_eject_only_targets_validated_mount(self):
        with patch('server.subprocess.run') as run:
            run.return_value.stdout='/dev/sdb\n'; run.return_value.returncode=0
            self.desk.eject()
            self.assertEqual(run.call_args.args[0], ['udisksctl','unmount','--block-device','/dev/sdb'])
        with patch('server.subprocess.run') as run:
            run.return_value.stdout='overlay\n'; run.return_value.returncode=0
            with self.assertRaises(ValueError): self.desk.eject()
            self.assertEqual(run.call_count,1)

    def test_http_origin_token_and_export(self):
        self.desk.import_notes(parse_clippings(CLIPPINGS)[0])
        server = make_server(self.desk, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        port=server.server_address[1]
        try:
            def request(method,path,headers=None,body=None):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                conn.request(method,path,body=body,headers=headers or {})
                response=conn.getresponse(); result=(response.status,response.read()); conn.close(); return result
            self.assertEqual(request('GET','/api/state')[0],200)
            self.assertEqual(request('GET','/api/state',{'Host':'evil.example'})[0],403)
            self.assertEqual(request('GET','/api/state',{'Origin':'https://evil.example'})[0],403)
            self.assertEqual(request('POST','/api/scan')[0],403)
            headers={'X-Desk-Token':self.desk.token}
            status,body=request('POST','/api/export',headers)
            self.assertEqual(status,200)
            self.assertEqual(len(zipfile.ZipFile(io.BytesIO(body)).namelist()),2)
            self.assertEqual(request('POST','/api/upload?name=..%2Fbad.txt',headers,b'data')[0],400)
            self.assertEqual(request('GET','/../../etc/passwd')[0],404)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_container_eject_disabled_and_host_binding_keeps_access_checks(self):
        self.desk.can_eject = False
        with patch('server.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, '宿主机'):
                self.desk.eject()
            run.assert_not_called()
        server = make_server(self.desk, 0, host='0.0.0.0')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request('GET', '/api/state')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertFalse(json.loads(response.read())['can_eject'])
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            conn.request('GET', '/api/state', headers={'Host': 'evil.example'})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
