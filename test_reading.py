"""Reading-cache fixtures match the inspected Kindle 11 firmware 5.19.2.0.1 schema."""
from contextlib import closing
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import Desk, make_server


def milliseconds(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def session(start='2025-10-19T13:00:00', end='2025-10-19T13:10:00', **changes):
    payload = {'asin': 'fixture-book', 'is_complete': True,
               'start_timestamp': milliseconds(start), 'end_timestamp': milliseconds(end)}
    payload.update(changes)
    return json.dumps({'schema_name': 'reading_session', 'schema_version': 0, 'payload': payload})


class ReadingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mount = self.root / 'Kindle'
        (self.mount / 'documents').mkdir(parents=True)
        self.source = self.mount / 'system/fmcache/fmcache.db'
        self.source.parent.mkdir(parents=True)
        self.desk = Desk(self.mount, self.root / 'data', require_mount=False)

    def tearDown(self):
        self.temp.cleanup()

    def cache(self, rows):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS reading_sessions(id INTEGER PRIMARY KEY,encoded_size INTEGER NOT NULL,session TEXT NOT NULL,dummy_session INTEGER)')
            db.execute('DELETE FROM reading_sessions')
            for i, raw in enumerate(rows):
                db.execute('INSERT INTO reading_sessions VALUES (?,?,?,?)', (i, len(raw), raw, None))

    def test_device_import_is_read_only_idempotent_and_survives_cache_cleanup_and_restart(self):
        self.cache([session(), session()])
        before = self.source.read_bytes()
        result = self.desk.scan()
        self.assertEqual(result['reading_added'], 1)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.desk.scan()['reading_added'], 0)
        activity = self.desk.reading_activity(2025)
        self.assertEqual(activity['days'], {'2025-10-19': 600})
        self.assertEqual(activity['active_days'], 1)
        self.cache([])
        self.assertEqual(self.desk.scan()['reading_added'], 0)
        self.assertEqual(self.desk.reading_activity(2025)['seconds'], 600)
        self.mount.rename(self.root / 'disconnected')
        restored = Desk(self.mount, self.desk.data, require_mount=False)
        restored.scan()
        self.assertEqual(restored.reading_activity(2025)['days'], activity['days'])
        self.assertEqual(restored.reading_activity(2025)['sessions'], 1)

    def test_only_valid_completed_sessions_are_imported_and_book_identity_not_row_id_deduplicates(self):
        unknown = json.loads(session())
        unknown['schema_version'] = 9
        self.cache([session(), session(is_complete=False), session(start_timestamp=True), session(start_timestamp='bad'),
                    session(end='2025-10-19T12:00:00'), session(asin=''), '{}', 'null', '{bad',
                    json.dumps(unknown), session(asin='second-book'), session(end_timestamp=99999999999999)])
        warnings = []
        self.assertEqual(self.desk.import_reading_sessions(warnings), 2)
        self.assertTrue(any('无效' in warning for warning in warnings))
        result = self.desk.reading_activity(2025)
        self.assertEqual(result['sessions'], 2)
        # Same time on two books cannot double the time spent on the device.
        self.assertEqual(result['seconds'], 600)
        self.cache([session(start='2025-10-20T13:00:00', end='2025-10-20T13:01:00')])
        self.assertEqual(self.desk.import_reading_sessions([]), 1)
        self.assertEqual(self.desk.reading_activity(2025)['seconds'], 660)

    def test_overlaps_midnight_year_boundary_and_leap_day(self):
        self.cache([
            session(start='2024-12-31T23:50:00', end='2025-01-01T00:10:00'),
            session(start='2025-01-01T00:05:00', end='2025-01-01T00:20:00'),
            session(start='2024-02-29T23:59:30', end='2024-03-01T00:00:30'),
        ])
        self.desk.scan()
        old = self.desk.reading_activity(2024)
        new = self.desk.reading_activity(2025)
        self.assertEqual(old['days'], {'2024-02-29': 30, '2024-03-01': 30, '2024-12-31': 600})
        self.assertEqual(new['days'], {'2025-01-01': 1200})
        self.assertEqual(old['first_date'], '2024-02-29')
        self.assertEqual(new['last_date'], '2025-01-01')
        self.assertIn(2024, new['years'])
        self.assertEqual(self.desk.reading_activity(2023)['active_days'], 0)
        for bad in [2006, 2101, True, '2025', 2025.5]:
            with self.subTest(year=bad), self.assertRaises(ValueError):
                self.desk.reading_activity(bad)

    def test_local_midnight_respects_daylight_saving(self):
        if not hasattr(time, 'tzset'):
            self.skipTest('tzset unavailable')
        try:
            with patch.dict(os.environ, {'TZ': 'America/New_York'}):
                time.tzset()
                self.cache([session(start='2025-03-09T00:00:00', end='2025-03-10T00:00:00')])
                self.desk.scan()
                self.assertEqual(self.desk.reading_activity(2025)['days'], {'2025-03-09': 23 * 3600})
        finally:
            time.tzset()

    def test_missing_and_malformed_cache_preserves_previous_history(self):
        self.cache([session()])
        self.desk.scan()
        self.source.unlink()
        self.assertTrue(any('没有可用' in warning for warning in self.desk.scan()['warnings']))
        self.source.write_bytes(b'not a database')
        result = self.desk.scan()
        self.assertTrue(any('不兼容' in warning for warning in result['warnings']))
        self.assertEqual(self.desk.reading_activity(2025)['seconds'], 600)
        self.assertEqual(result['reading_added'], 0)
        self.source.unlink()
        external = self.root / 'outside.db'
        external.write_bytes(b'outside')
        self.source.symlink_to(external)
        warnings = []
        self.assertEqual(self.desk.import_reading_sessions(warnings), 0)
        self.assertTrue(warnings)
        self.assertEqual(external.read_bytes(), b'outside')

    def test_wal_snapshot_keeps_committed_sessions_without_writing_device(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE reading_sessions(session TEXT)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('INSERT INTO reading_sessions VALUES (?)', (session(),))
            db.commit()
            before = {p.name: p.read_bytes() for p in self.source.parent.iterdir()}
            self.assertEqual(self.desk.import_reading_sessions([]), 1)
            self.assertEqual({p.name: p.read_bytes() for p in self.source.parent.iterdir()}, before)
        self.assertEqual(self.desk.reading_activity(2025)['seconds'], 600)

    def test_http_reading_respects_origin_and_serves_script(self):
        self.cache([session()]); self.desk.scan()
        server = make_server(self.desk, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path, headers=None):
                with closing(http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)) as conn:
                    conn.request('GET', path, headers=headers or {})
                    response = conn.getresponse()
                    return response.status, response.read()
            status, body = get('/api/reading?year=2025')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['seconds'], 600)
            self.assertEqual(get('/api/reading?year=nope')[0], 409)
            self.assertEqual(get('/api/reading?year=2101')[0], 409)
            self.assertEqual(get('/api/reading', {'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(get('/api/reading', {'Host': 'evil.example'})[0], 403)
            self.assertEqual(get('/reading.js')[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
