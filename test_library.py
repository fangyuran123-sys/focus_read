from contextlib import closing, contextmanager
import http.client
import json
import sqlite3
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import Desk, make_server
from library import declares_cover


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mount = self.root / 'device'
        self.desk = Desk(self.mount, self.root / 'data', require_mount=False)
        self.library = self.desk.library
        self.library.metadata_tool = None
        self.source = self.root / 'source.txt'
        self.source.write_text('这是原始内容。', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def import_book(self):
        return self.library.import_file(self.source, 'Book.txt')['book']

    def test_existing_database_is_upgraded_without_losing_archived_data(self):
        old_data = self.root / 'old-data'
        old_data.mkdir()
        with closing(sqlite3.connect(old_data / 'desk.sqlite3')) as db, db:
            db.executescript('''
                CREATE TABLE notes(id TEXT PRIMARY KEY, payload TEXT NOT NULL, imported REAL NOT NULL, batch TEXT NOT NULL);
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE transfers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL);
            ''')
            db.execute('INSERT INTO notes VALUES (?,?,?,?)', ('old', json.dumps({'text': 'archived'}), 1, 'old-batch'))
            db.execute('INSERT INTO transfers(name,size,created) VALUES (?,?,?)', ('old-book.txt', 42, 1))
        upgraded = Desk(self.mount, old_data, require_mount=False)
        self.assertEqual(upgraded.view()['notes'][0]['text'], 'archived')
        self.assertEqual(upgraded.view()['transfers'][0]['name'], 'old-book.txt')
        self.assertEqual(upgraded.library.listing()['total'], 0)

    def test_offline_import_copy_and_hash_dedup_survive_restart(self):
        book = self.import_book()
        self.assertFalse(self.desk.connected())
        again = self.library.import_file(self.source, 'Renamed.txt')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['book']['id'], book['id'])
        self.source.unlink()
        desk = Desk(self.mount, self.root / 'data', require_mount=False)
        stored = desk.library.book(book['id'])
        info, path = desk.library.file(stored['files'][0]['id'])
        self.assertEqual(path.read_text(), '这是原始内容。')
        self.assertEqual(info['name'], 'Book.txt')
        self.assertEqual(desk.library.listing()['total'], 1)

    def test_full_data_backup_restores_books_files_and_existing_notes(self):
        book = self.import_book()
        self.desk.import_notes([{'id': 'legacy-note', 'title': 'Old title', 'text': 'old note'}])
        self.desk.write_setting('snapshot', {'books': [], 'last_scan': 123, 'warnings': []})
        restored_data = self.root / 'restored-data'
        shutil.copytree(self.desk.data, restored_data)
        shutil.rmtree(self.desk.data)
        restored = Desk(self.mount, restored_data, require_mount=False)
        self.assertEqual(restored.library.book(book['id']), book)
        self.assertEqual(restored.library.file(book['files'][0]['id'])[1].read_bytes(), self.source.read_bytes())
        self.assertEqual(restored.view()['notes'][0]['text'], 'old note')
        self.assertEqual(restored.view()['last_scan'], 123)

    def test_multi_format_and_metadata_do_not_change_identity_or_files(self):
        book = self.import_book()
        pdf = self.root / 'source.pdf'
        pdf.write_bytes(b'%PDF fixture')
        result = self.library.import_file(pdf, 'Book.pdf', book['id'])
        self.assertEqual(len(result['book']['files']), 2)
        updated = self.library.update({**book, 'title': '新书名', 'author': '作者', 'tags': ['文学', '文学', '随笔'],
                                       'status': 'reading', 'description': '简介', 'review': '我的感想'})
        self.assertEqual(updated['id'], book['id'])
        self.assertEqual(updated['tags'], ['文学', '随笔'])
        self.assertEqual(len(updated['files']), 2)
        self.assertEqual(self.library.listing(query='作者', tag='文学', status='reading')['total'], 1)
        self.assertEqual(self.library.listing(query='Book')['total'], 0)
        self.assertEqual(self.library.listing()['counts'], {'reading': 1})

    def test_search_is_literal_paginated_and_filters_do_not_inject_sql(self):
        for i in range(38):
            self.source.write_text('unique ' + str(i))
            self.library.import_file(self.source, f'Book {i}.txt')
        self.assertEqual(len(self.library.listing()['books']), 36)
        self.assertEqual(len(self.library.listing(page=2)['books']), 2)
        self.assertEqual(self.library.listing(query='%')['total'], 0)
        self.assertEqual(self.library.listing(query="' OR 1=1 --")['total'], 0)
        for kwargs in [{'page': 0}, {'page': 100001}, {'status': 'bad'}, {'query': 'a' * 201}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.library.listing(**kwargs)

    def test_listing_has_bounded_queries_and_excludes_detail_payload(self):
        for i in range(38):
            self.source.write_text(str(i))
            book = self.import_book()
            self.library.update({**book, 'review': 'private review', 'description': 'long description'})
        queries = []
        original_db = self.desk.db
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(queries.append)
                yield db
        with patch.object(self.desk, 'db', traced_db):
            result = self.library.listing()
        self.assertEqual(len(result['books']), 36)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 6)
        self.assertEqual(result['books'][0]['formats'], ['TXT'])
        self.assertNotIn('review', result['books'][0])
        self.assertNotIn('description', result['books'][0])
        self.assertNotIn('files', result['books'][0])
        last = self.library.listing(page=999)
        self.assertEqual((last['page'], last['pages'], len(last['books'])), (2, 2, 2))

    def test_reading_columns_upgrade_existing_books_without_inventing_dates(self):
        book = self.import_book()
        with self.desk.db() as db:
            db.execute("UPDATE library_books SET status='finished' WHERE id=?", (book['id'],))
            for name in ['progress', 'current_page', 'rating', 'finished_at']:
                db.execute('ALTER TABLE library_books DROP COLUMN ' + name)
        upgraded = Desk(self.mount, self.desk.data, require_mount=False).library
        upgraded.metadata_tool = None
        stored = upgraded.book(book['id'])
        self.assertEqual((stored['progress'], stored['current_page'], stored['rating'], stored['finished_at']), (0, 0, 0, None))
        self.assertEqual(upgraded.listing()['stats']['finished'], 1)
        self.assertEqual(upgraded.listing()['stats']['finished_year'], 0)
        updated = upgraded.update({**stored, 'review': '补写旧笔记'})
        self.assertIsNone(updated['finished_at'])
        self.assertEqual(upgraded.file(book['files'][0]['id'])[1].read_bytes(), self.source.read_bytes())

    def test_progress_rating_sort_filter_and_year_stats(self):
        first = self.import_book()
        first = self.library.update({**first, 'status': 'reading', 'progress': 42, 'current_page': 88, 'rating': 5})
        self.source.write_text('second book')
        second = self.import_book()
        second = self.library.update({**second, 'rating': 3})
        self.assertEqual([b['id'] for b in self.library.listing(sort='rating')['books']], [first['id'], second['id']])
        filtered = self.library.listing(rating=4)
        self.assertEqual(filtered['total'], 1)
        self.assertEqual((filtered['books'][0]['progress'], filtered['books'][0]['current_page']), (42, 88))
        self.desk.import_notes([{'id': 'note', 'title': 'Book', 'text': 'quote'}])
        finished = self.library.update({**first, 'status': 'finished'})
        self.assertEqual(finished['progress'], 100)
        self.assertIsNotNone(finished['finished_at'])
        saved = self.library.update({**finished, 'review': '读后感'})
        self.assertEqual(saved['finished_at'], finished['finished_at'])
        stats = self.library.listing(query='no match', rating=5)['stats']
        self.assertEqual((stats['finished_year'], stats['finished'], stats['reading'], stats['notes']), (1, 1, 0, 1))
        with self.desk.db() as db:
            db.execute('UPDATE library_books SET finished_at=? WHERE id=?', (time.mktime((time.localtime().tm_year - 1, 6, 1, 0, 0, 0, 0, 0, -1)), first['id']))
        self.assertEqual(self.library.listing()['stats']['finished_year'], 0)
        reopened = self.library.update({**saved, 'status': 'reading', 'progress': 10})
        self.assertIsNone(reopened['finished_at'])
        self.assertEqual(self.library.listing()['stats']['reading'], 1)
        # Older clients omit new fields; preserve the stored values.
        payload = {k: v for k, v in reopened.items() if k not in {'progress', 'current_page', 'rating'}}
        self.assertEqual(self.library.update(payload)['rating'], 5)
        for key, bad in [('rating', 6), ('rating', True), ('progress', -1), ('progress', 12.5), ('current_page', '2'), ('current_page', 1000001)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.library.update({**reopened, key: bad})
        for rating in [-1, 6, True, '4']:
            with self.subTest(rating=rating), self.assertRaises(ValueError):
                self.library.listing(rating=rating)

    def test_delete_keeps_sources_device_notes_and_optionally_library_copies(self):
        book = self.import_book()
        pdf = self.root / 'other.pdf'
        pdf.write_bytes(b'%PDF other')
        book = self.library.import_file(pdf, 'Book.pdf', book['id'])['book']
        self.desk.import_notes([{'id': 'note', 'title': 'Book', 'text': 'quote'}])
        (self.mount / 'documents').mkdir(parents=True)
        self.library.send(book['files'][0]['id'], '')
        device_copy = next((self.mount / 'documents').iterdir())
        paths = [self.library.file(f['id'])[1] for f in book['files']]
        cover = self.library.covers / (book['id'] + '.jpg')
        cover.write_bytes(b'cover')
        self.library.delete(book['id'])
        self.assertEqual(self.library.listing()['total'], 0)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertFalse(cover.exists())
        book = self.import_book()
        book = self.library.import_file(pdf, 'Book.pdf', book['id'])['book']
        converted = self.root / 'converted.azw3'
        converted.write_bytes(b'converted copy')
        book = self.library.import_file(converted, 'Book.azw3', book['id'], generated_from=book['files'][0]['id'])['book']
        self.source.write_text('another surviving book')
        survivor = self.import_book()
        survivor_path = self.library.file(survivor['files'][0]['id'])[1]
        paths = [self.library.file(f['id'])[1] for f in book['files']]
        self.library.delete(book['id'], True)
        self.assertTrue(survivor_path.exists())
        self.assertEqual(self.library.book(survivor['id'])['title'], survivor['title'])
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertTrue(self.source.exists())
        self.assertTrue(pdf.exists())
        self.assertTrue(device_copy.exists())
        self.assertEqual(len(self.desk.view()['notes']), 1)
        with self.assertRaises(ValueError):
            self.library.delete(book['id'])
        self.assertEqual(self.library.listing()['stats']['finished_year'], 0)

    def test_delete_rolls_back_before_touching_files_and_rejects_symlinks(self):
        book = self.import_book()
        _, path = self.library.file(book['files'][0]['id'])
        with self.assertRaises(ValueError):
            self.library.delete(book['id'], 'true')
        with self.desk.db() as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON library_books BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.delete(book['id'], True)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.library.book(book['id'])['files']), 1)
        with self.desk.db() as db:
            db.execute('DROP TRIGGER fail_delete')
        path.unlink()
        path.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.library.delete(book['id'], True)
        self.assertTrue(self.source.exists())
        path.unlink()
        # A missing file must not prevent removing a broken library record.
        self.library.delete(book['id'], True)
        self.assertEqual(self.library.listing()['total'], 0)

    def test_delete_reports_cleanup_failure_without_restoring_broken_metadata(self):
        book = self.import_book()
        with patch('library.Path.unlink', side_effect=PermissionError('read only')):
            result = self.library.delete(book['id'], True)
        self.assertTrue(result['cleanup_failed'])
        self.assertIn('未能清理', result['message'])
        self.assertEqual(self.library.listing()['total'], 0)
        self.assertTrue(self.source.exists())

    def test_metadata_import_reads_opf_without_touching_source(self):
        self.library.metadata_tool = '/test/ebook-meta'
        before = self.source.read_bytes()
        opf_calls = []
        def extract(args, **kwargs):
            self.assertEqual(args[0], '/test/ebook-meta')
            if '--to-opf' in args:
                opf_calls.append(args)
                self.assertEqual(kwargs['timeout'], 20)
                Path(args[3]).write_text('''<package xmlns:dc="http://purl.org/dc/elements/1.1/">
                    <metadata><dc:title>真实书名</dc:title><dc:creator>作者甲</dc:creator>
                    <dc:creator>作者乙</dc:creator><dc:creator>Unknown</dc:creator></metadata></package>''')
                return
            raise subprocess.CalledProcessError(1, 'ebook-meta')
        with patch('library.subprocess.run', side_effect=extract):
            result = self.library.import_file(self.source, 'random-filename.epub')
            self.assertEqual(len(opf_calls), 1)
            self.assertEqual(result['book']['title'], '真实书名')
            self.assertEqual(result['book']['author'], '作者甲、作者乙')
            self.assertEqual(result['warning'], '')
            self.library.import_file(self.source, 'again.epub')
            self.assertEqual(len(opf_calls), 1)
        self.assertEqual(self.source.read_bytes(), before)

    def test_cover_is_extracted_saved_and_served_once(self):
        self.library.metadata_tool = '/test/ebook-meta'
        cover_calls = []
        def extract(args, **kwargs):
            if '--to-opf' in args:
                Path(args[3]).write_text('<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>有封面</dc:title></metadata></package>')
                return
            cover_calls.append(args)
            Path(args[3]).write_bytes(b'\xff\xd8\xff\xe0fake jpeg')
        with patch('library.subprocess.run', side_effect=extract):
            result = self.library.import_file(self.source, 'cover.epub')
            self.assertTrue(result['book']['cover'])
            self.assertEqual(len(cover_calls), 1)
            self.assertEqual(cover_calls[0][0], '/test/ebook-meta')
            self.assertIn('--get-cover', cover_calls[0])
            again = self.library.book(result['book']['id'])
            self.assertTrue(again['cover'])
            self.assertEqual(len(cover_calls), 1)
        self.assertEqual(self.library.cover_path(result['book']['id']).read_bytes(), b'\xff\xd8\xff\xe0fake jpeg')
        self.assertTrue(self.library.listing()['books'][0]['cover'])

    def test_missing_or_invalid_cover_marks_book_once_and_never_blocks(self):
        self.library.metadata_tool = '/test/ebook-meta'
        calls = []
        def no_cover(args, **kwargs):
            if '--to-opf' in args:
                Path(args[3]).write_text('<package><metadata/></package>')
                return
            calls.append(args)
        with patch('library.subprocess.run', side_effect=no_cover):
            result = self.library.import_file(self.source, 'plain.epub')
            self.assertFalse(result['book']['cover'])
            self.library.book(result['book']['id'])
            self.assertEqual(len(calls), 1)
        self.assertIsNone(self.library.cover_path(result['book']['id']))
        self.assertFalse(self.library.listing()['books'][0]['cover'])

    def test_covers_are_not_read_for_unsupported_formats(self):
        self.library.metadata_tool = '/test/ebook-meta'
        with patch('library.subprocess.run') as run:
            result = self.library.import_file(self.source, 'notes.txt')
            run.assert_not_called()
        self.assertFalse(result['book']['cover'])

    def test_metadata_failure_timeout_and_malformed_output_do_not_block_import(self):
        self.library.metadata_tool = '/test/ebook-meta'
        for error in [OSError('missing'), subprocess.TimeoutExpired('ebook-meta', 20), subprocess.CalledProcessError(1, 'ebook-meta')]:
            self.source.write_text(str(error))
            with self.subTest(error=error), patch('library.subprocess.run', side_effect=error):
                result = self.library.import_file(self.source, 'Fallback.pdf')
                self.assertEqual(result['book']['title'], 'Fallback')
                self.assertEqual(result['book']['author'], '')
                self.assertTrue(result['warning'])
                self.assertFalse(result['book']['cover'])
        def malformed(args, **kwargs):
            Path(args[3]).write_text('<not xml')
        self.source.write_text('malformed xml')
        with patch('library.subprocess.run', side_effect=malformed):
            result = self.library.import_file(self.source, 'Fallback.epub')
            self.assertTrue(result['warning'])

    def test_metadata_never_overwrites_existing_book_when_adding_format(self):
        book = self.import_book()
        book = self.library.update({**book, 'title': '手动书名', 'author': '手动作者'})
        self.source.write_text('new format')
        self.library.metadata_tool = '/test/ebook-meta'
        with patch('library.subprocess.run') as run:
            result = self.library.import_file(self.source, 'Book.pdf', book['id'])
            run.assert_not_called()
        self.assertEqual(result['book']['title'], '手动书名')
        self.assertEqual(result['book']['author'], '手动作者')

    def test_epub_cover_is_only_extracted_when_the_package_declares_one(self):
        import zipfile as zf
        def build(path, meta):
            with zf.ZipFile(path, 'w') as archive:
                archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                archive.writestr('content.opf', '<package><metadata>' + meta + '</metadata></package>')
        declared = self.root / 'declared.epub'; build(declared, '<meta name="cover" content="c"/>')
        property_cover = self.root / 'property.epub'; build(property_cover, '<item id="c" properties="cover-image"/>')
        plain = self.root / 'plain.epub'; build(plain, '')
        self.assertTrue(declares_cover(declared))
        self.assertTrue(declares_cover(property_cover))
        self.assertFalse(declares_cover(plain))
        self.assertTrue(declares_cover(self.source), 'unreadable archives fall through to calibre')
        self.library.metadata_tool = '/test/ebook-meta'
        cover_calls = []
        def record(args, **kwargs):
            if '--to-opf' in args:
                Path(args[3]).write_text('<package><metadata/></package>')
                return
            cover_calls.append(args)
        with patch('library.subprocess.run', side_effect=record):
            result = self.library.import_file(plain, 'No cover.epub')
        self.assertEqual(cover_calls, [])
        self.assertFalse(result['book']['cover'])
        self.assertTrue((self.library.covers / (result['book']['id'] + '.none')).exists())

    def test_validation_and_missing_files(self):
        book = self.import_book()
        for name in ['../bad.txt', 'bad.exe', '', 'x' * 221 + '.txt']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.library.import_file(self.source, name)
        for change in [{'title': ''}, {'author': []}, {'status': []}, {'status': 'bad'}, {'tags': 'text'}, {'tags': ['a' * 41]}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.library.update({**book, **change})
        with self.assertRaises(ValueError):
            self.library.import_file(self.source, 'Other.txt', 'missing')
        info, path = self.library.file(book['files'][0]['id'])
        path.unlink()
        with self.assertRaises(ValueError):
            self.library.file(info['id'])
        self.assertEqual(self.library.book(book['id'])['id'], book['id'])

    def test_duplicate_cannot_be_silently_attached_to_another_book(self):
        first = self.import_book()
        self.source.write_text('different')
        second = self.import_book()
        _, original = self.library.file(first['files'][0]['id'])
        with self.assertRaisesRegex(ValueError, '另一本'):
            self.library.import_file(original, 'Copy.txt', second['id'])
        self.assertEqual(len(self.library.book(second['id'])['files']), 1)

    def test_symlinks_are_not_read_as_library_files(self):
        book = self.import_book()
        info, path = self.library.file(book['files'][0]['id'])
        path.unlink()
        path.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.library.file(info['id'])

    def test_import_database_failure_never_destroys_source_and_retry_recovers(self):
        with self.desk.db() as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON library_files BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_book()
        self.assertEqual(self.source.read_text(), '这是原始内容。')
        self.assertEqual(self.library.listing()['total'], 0)
        with self.desk.db() as db:
            db.execute('DROP TRIGGER fail_insert')
        result = self.library.import_file(self.source, 'Book.txt')
        self.assertFalse(result['duplicate'])
        self.assertEqual(self.library.book(result['book']['id'])['title'], 'Book')

    def test_copy_verification_does_not_commit_corrupt_file(self):
        def corrupt(src, out):
            out.write(b'corrupted copy')
        with patch('library.shutil.copyfileobj', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                self.import_book()
        self.assertEqual(list(self.library.root.iterdir()), [])
        self.assertEqual(self.library.listing()['total'], 0)
        self.assertEqual(self.source.read_text(), '这是原始内容。')

    def test_send_offline_rejected_then_original_sent_without_mutation(self):
        book = self.import_book()
        fid = book['files'][0]['id']
        with self.assertRaisesRegex(ValueError, '断开'):
            self.library.send(fid)
        (self.mount / 'documents').mkdir(parents=True)
        self.library.send(fid, '')
        self.assertEqual((self.mount / 'documents/Book.txt').read_bytes(), self.source.read_bytes())
        stored = self.library.book(book['id'])['files'][0]
        self.assertIsNotNone(stored['sent_at'])
        self.assertEqual(stored['sent_path'], 'documents/Book.txt')
        with self.assertRaisesRegex(ValueError, '同名'):
            self.library.send(fid, '')
        self.assertEqual(self.library.file(fid)[1].read_bytes(), self.source.read_bytes())

    def test_epub_conversion_retains_original_caches_output_and_records_destination(self):
        (self.mount / 'documents').mkdir(parents=True)
        epub = self.root / 'original.epub'
        epub.write_bytes(b'original epub fixture')
        book = self.library.import_file(epub, 'Book.epub')['book']
        fid = book['files'][0]['id']
        self.desk.converter = '/fake/ebook-convert'
        def convert(args, **kwargs):
            Path(args[2]).write_bytes(b'converted fixture')
            return type('Result', (), {'returncode': 0})()
        with patch('server.subprocess.run', side_effect=convert) as run:
            self.library.send(fid)
            (self.mount / 'documents/Kindle Desk/Book.azw3').unlink()
            self.library.send(fid)
            self.assertEqual(run.call_count, 1)
        stored = self.library.book(book['id'])
        self.assertEqual(len(stored['files']), 2)
        self.assertEqual(self.library.file(fid)[1].read_bytes(), b'original epub fixture')
        converted = next(f for f in stored['files'] if f['generated_from'])
        self.assertEqual(converted['generated_from'], fid)
        self.assertEqual(converted['sent_path'], 'documents/Kindle Desk/Book.azw3')
        self.assertEqual(self.library.file(converted['id'])[1].read_bytes(), b'converted fixture')

    def test_conversion_failure_keeps_original(self):
        (self.mount / 'documents').mkdir(parents=True)
        book = self.library.import_file(self.source, 'Book.epub')['book']
        fid = book['files'][0]['id']
        with self.assertRaisesRegex(ValueError, '未安装'):
            self.library.send(fid)
        self.assertEqual(len(self.library.book(book['id'])['files']), 1)
        self.assertEqual(self.library.file(fid)[1].read_bytes(), self.source.read_bytes())

    def test_http_library_workflow_security_and_streamed_download(self):
        server = make_server(self.desk, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                result = response.status, response.read()
                conn.close()
                return result
            token = {'X-Desk-Token': self.desk.token}
            self.assertEqual(request('POST', '/api/library/import?name=Book.txt', b'content')[0], 403)
            self.assertEqual(request('GET', '/api/library', headers={'Host': 'evil.example'})[0], 403)
            self.assertEqual(request('POST', '/api/library/import?name=Book.txt', b'content', {**token, 'Origin': 'https://evil.example'})[0], 403)
            status, body = request('POST', '/api/library/import?name=Book.txt', b'content', token)
            self.assertEqual(status, 200)
            book = json.loads(body)['book']
            status, body = request('GET', '/api/library/file?id=' + book['files'][0]['id'])
            self.assertEqual((status, body), (200, b'content'))
            book.update(author='Test author', tags=['tag'], status='finished', rating=4, current_page=120)
            self.assertEqual(request('POST', '/api/library/update', json.dumps(book), token)[0], 200)
            status, body = request('GET', '/api/library?q=Test&status=finished&tag=tag')
            self.assertEqual(json.loads(body)['total'], 1)
            self.assertEqual(request('GET', '/api/library?page=bad')[0], 409)
            self.assertEqual(request('GET', '/api/library/file?id=../../etc/passwd')[0], 409)
            self.assertEqual(request('POST', '/api/library/update', '[]', token)[0], 400)
            self.assertEqual(request('POST', '/api/library/send', json.dumps({'id': book['files'][0]['id']}), token)[0], 400)
            for path in ['/', '/library.js', '/library.css']:
                self.assertEqual(request('GET', path)[0], 200)
            self.assertEqual(request('GET', '/api/cover?book=' + book['id'])[0], 404)
            self.assertEqual(request('GET', '/api/library?sort=bad')[0], 409)
            status, body = json.loads(request('GET', '/api/library?sort=title')[1]), None
            self.assertEqual(status['total'], 1)
            self.assertEqual(json.loads(request('GET', '/api/library?rating=5')[1])['total'], 0)
            self.assertEqual(json.loads(request('GET', '/api/library?sort=rating&rating=4')[1])['total'], 1)
            self.assertEqual(request('GET', '/api/library?rating=9')[0], 409)
            payload = json.dumps({'id': book['id'], 'delete_files': True})
            self.assertEqual(request('POST', '/api/library/delete', payload)[0], 403)
            self.assertEqual(request('POST', '/api/library/delete', payload, {**token, 'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(request('POST', '/api/library/delete', '[]', token)[0], 400)
            self.assertEqual(request('POST', '/api/library/delete', payload, token)[0], 200)
            self.assertEqual(json.loads(request('GET', '/api/library')[1])['total'], 0)
            self.assertEqual(request('GET', '/api/library/file?id=' + book['files'][0]['id'])[0], 409)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
