#!/usr/bin/env python3
"""A loopback-only Kindle desk. Python standard library; calibre converts EPUB."""
import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from library import Library, safe_name
from moaan import Moaan

BASE = Path(__file__).resolve().parent
FORMATS = {'.azw', '.azw3', '.mobi', '.pdf', '.txt', '.kfx', '.prc'}
UPLOAD_FORMATS = {'.azw', '.azw3', '.mobi', '.pdf', '.txt', '.epub'}
MAX_UPLOAD = 200 * 1024 * 1024
SIDECARS = ('.sdr', '.mbp', '.mbp1', '.mbs', '.pdr', '.apnx', '.phl')
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/library.js': ('library.js', 'text/javascript; charset=utf-8'),
          '/library.css': ('library.css', 'text/css; charset=utf-8'), '/reading.js': ('reading.js', 'text/javascript; charset=utf-8'),
          '/moaan.js': ('moaan.js', 'text/javascript; charset=utf-8'), '/icon.svg': ('icon.svg', 'image/svg+xml')}


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def contained(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('文件路径超出了允许的目录。')
    return resolved


def folder_name(name):
    if not isinstance(name, str):
        raise ValueError('分类名称必须是文字。')
    safe_name(name)
    if name.startswith('.') or name.lower().endswith('.sdr') or name.endswith((' ', '.')) or any(c in name for c in '<>:"|?*'):
        raise ValueError('分类名称不能是隐藏目录、书籍附件目录，或包含特殊字符。')
    return name


def rename_no_replace(source, target):
    # Linux renameat2 prevents a concurrent file manager from being overwritten.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def parse_clippings(text):
    result, skipped = [], 0
    for block in re.split(r'(?m)^={10,}\s*$', text.lstrip('\ufeff')):
        lines = block.strip().splitlines()
        if not lines:
            continue
        if len(lines) < 2 or not lines[1].lstrip().startswith('-'):
            skipped += 1
            continue
        title, meta = lines[0].strip(), lines[1].strip()
        body = '\n'.join(lines[2:]).strip()
        kind = 'note' if re.search(r'note|笔记|筆記', meta, re.I) else 'bookmark' if re.search(r'bookmark|书签|書籤', meta, re.I) else 'highlight'
        if not body and kind != 'bookmark':
            skipped += 1
            continue
        location = meta.split('|', 1)[0].lstrip('- ').strip()
        result.append({'id': digest('\0'.join((title, kind, location, body))), 'title': title,
                       'kind': kind, 'text': body, 'location': location, 'metadata': meta,
                       'source': 'clippings', 'pending': False})
    return result, skipped


def markdown_zip(notes):
    grouped = {}
    for note in notes:
        grouped.setdefault(note['title'], []).append(note)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for title, items in grouped.items():
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)[:70] or '未命名'
            lines = ['# ' + title, '', '由 Kindle Desk 导出；保留在本地的摘录记录。', '']
            for item in items:
                lines += ['## ' + {'highlight': '高亮', 'note': '笔记', 'bookmark': '书签'}[item['kind']], '']
                if item['pending']:
                    lines += ['高亮位置已读取，正文尚未解析。', '']
                else:
                    lines += ['> ' + line for line in item['text'].splitlines()] + ['']
                lines += [item['location'], '', '来源：' + item['source'], '']
            archive.writestr(filename + '-' + digest(title)[:8] + '.md', '\n'.join(lines))
    return buf.getvalue()


class Desk:
    def __init__(self, mount, data, converter=None, require_mount=True, can_eject=True):
        self.mount, self.data = Path(mount).resolve(), Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.converter, self.require_mount = converter, require_mount
        self.can_eject = can_eject
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.db_path = self.data / 'desk.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, payload TEXT NOT NULL, imported REAL NOT NULL, batch TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reading_sessions(id TEXT PRIMARY KEY, started INTEGER NOT NULL, ended INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL);
            ''')
        self.status = self.read_setting('snapshot', {'books': [], 'last_scan': None, 'warnings': []})
        self.library = Library(self)
        self.moaan = Moaan(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read_setting(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def write_setting(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def connected(self):
        return self.mount.is_dir() and (not self.require_mount or os.path.ismount(self.mount)) and (self.mount / 'documents').is_dir()

    def device_path(self, relative):
        if not self.connected():
            raise ValueError('Kindle 已断开，请重新连接后重试。')
        return contained(self.mount / relative, self.mount)

    def folder_path(self, relative):
        if not isinstance(relative, str) or len(relative) > 1024:
            raise ValueError('分类路径无效。')
        root = self.device_path('documents')
        path = root
        for part in relative.split('/') if relative else []:
            path = path / folder_name(part)
            if path.is_symlink():
                raise ValueError('分类路径不能包含符号链接。')
        return contained(path, root)

    def create_folder(self, parent, name):
        with self.lock:
            folder = self.folder_path(parent)
            name = folder_name(name)
            if not folder.is_dir():
                raise ValueError('上级分类不存在，请刷新后重试。')
            if any(p.name.casefold() == name.casefold() for p in folder.iterdir()):
                raise ValueError('该位置已有同名文件或分类，请换一个名称。')
            (folder / name).mkdir()
            self.scan()
            return {'folder': '/'.join(filter(None, (parent, name))), 'message': '分类已创建在 Kindle 上。'}

    def move_book(self, book_id, destination):
        with self.lock:
            if any((self.data / 'moves').glob('*.json')):
                raise ValueError('有未完成的移动记录，请先检查本机 data/moves 中的恢复记录，再继续整理。')
            folder = self.folder_path(destination)
            if not folder.is_dir():
                raise ValueError('目标分类不存在，请刷新后重试。')
            book = next((b for b in self.status['books'] if b['id'] == book_id), None)
            if not book:
                raise ValueError('书架已变化，请刷新后重新选择图书。')
            parent = str(Path(book['path']).relative_to('documents').parent)
            source = self.folder_path('' if parent == '.' else parent) / book['name']
            if source.is_symlink() or not source.is_file():
                raise ValueError('原书籍已变化或不可用，请刷新后重试。')
            stat = source.stat()
            if stat.st_size != book['size'] or stat.st_mtime != book['modified']:
                raise ValueError('书籍在扫描后发生变化，请刷新后重试。')
            if source.parent == folder:
                return {'folder': destination, 'message': '这本书已在该分类中。'}
            companions = [source.with_suffix(ext) for ext in SIDECARS]
            companions = [p for p in companions if p.exists() or p.is_symlink()]
            if companions and any(p != source and p.stem == source.stem and p.suffix.lower() in FORMATS for p in source.parent.iterdir()):
                raise ValueError('这本书与同名的其他格式共用附件，请先备份并整理同名书籍。')
            plan = [(p, folder / p.name) for p in [*companions, source]]
            existing = {p.name.casefold() for p in folder.iterdir()}
            for src, dst in plan:
                if src.is_symlink():
                    raise ValueError('书籍附件包含符号链接，无法安全移动。')
                if dst.name.casefold() in existing:
                    raise ValueError('目标分类已有同名书籍或附件，原文件未移动。')
            history = self.data / 'moves'
            history.mkdir(exist_ok=True)
            journal = history / (secrets.token_hex(8) + '.json')
            # A book and its sidecars cannot move atomically; retain a recovery plan until complete.
            with journal.open('x', encoding='utf-8') as log:
                json.dump({'mount': str(self.mount), 'moves': [{'from': str(a.relative_to(self.mount)), 'to': str(b.relative_to(self.mount))} for a, b in plan]}, log, ensure_ascii=False, indent=2)
                log.flush()
                os.fsync(log.fileno())
            moved = []
            try:
                for src, dst in plan:
                    rename_no_replace(src, dst)
                    moved.append((src, dst))
            except OSError as error:
                recovered = True
                for src, dst in reversed(moved):
                    try:
                        rename_no_replace(dst, src)
                    except OSError:
                        recovered = False
                if not recovered:
                    raise ValueError('移动中断，部分文件仍在目标分类。请停止整理；本机 data/moves 中保留了原位置和目标位置的恢复记录。') from error
                journal.unlink()
                raise ValueError('移动失败，已回退本次改动。请检查连接、写入权限和同名文件后重试。') from error
            journal.unlink()
            try:
                self.scan()
            except (OSError, ValueError, sqlite3.Error):
                return {'folder': destination, 'message': '文件已移动，但重新读取失败。请确认连接后刷新书架。'}
            return {'folder': destination, 'message': '图书及关联附件已移动。安全弹出后，等待 Kindle 重新索引。'}

    def import_notes(self, notes):
        batch = secrets.token_hex(8)
        added = 0
        with self.db() as db:
            for note in notes:
                old = db.execute('SELECT id FROM notes WHERE id=?', (note['id'],)).fetchone()
                payload = json.dumps(note, ensure_ascii=False)
                if old:
                    db.execute('UPDATE notes SET payload=? WHERE id=?', (payload, note['id']))
                else:
                    db.execute('INSERT INTO notes VALUES (?,?,?,?)', (note['id'], payload, time.time(), batch))
                    added += 1
        if added:
            self.write_setting('last_batch', batch)
        return added

    def snapshot_db(self, source, folder):
        target = folder / source.name
        shutil.copyfile(contained(source, self.mount), target)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copyfile(contained(sidecar, self.mount), Path(str(target) + suffix))
        return target

    def database_notes(self, books, warnings):
        notes, stores = [], 0
        titles = {}
        for book in books:
            match = re.search(r'_([A-Z0-9]{10,32})$', Path(book['name']).stem)
            if match:
                titles[match[1]] = book['title']
        directory = self.device_path('system/ksdk/.annotations')
        for source in sorted(directory.glob('*/ksdk_annotation_v1.db')):
            stores += 1
            try:
                with tempfile.TemporaryDirectory(dir=self.data) as temp:
                    path = self.snapshot_db(source, Path(temp))
                    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
                    try:
                        merged = {}
                        for table, key in [('server_view', 'dataset_id'), ('nonsyncable_annotations', 'book_id'), ('local_edit', 'dataset_id')]:
                            cols = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
                            if not {'annotation_id', 'serialized_payload', 'dataset', key}.issubset(cols):
                                warnings.append('标注数据库结构有变化，部分记录未读取。')
                                continue
                            action = ',action' if 'action' in cols else ',NULL'
                            for aid, bid, kind, payload, action_value in db.execute('SELECT annotation_id,' + key + ',dataset,serialized_payload' + action + ' FROM ' + table + ' WHERE dataset IN (1,3)'):
                                merged[str(aid)] = (bid, kind, payload, action_value)
                        for aid, (bid, kind, payload, action_value) in merged.items():
                            # shortcut: local action codes need a real edited/deleted-note sample before deletion sync is supported.
                            if action_value not in (None, 0, '0', '', 'CREATE', 'UPDATE', 'create', 'update'):
                                warnings.append('部分本地标注操作尚未识别，保留旧导入记录。')
                                continue
                            p = json.loads(payload)
                            metadata = p.get('json_metadata') or '{}'
                            metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                            body = metadata.get('note_text', '') if kind == 3 else p.get('highlight_text', '')
                            book_id = str(bid).split('-', 1)[0]
                            pos = p.get('start_position') or {}
                            location = '内部位置 ' + str(pos.get('shortPosition', '未知'))
                            notes.append({'id': digest('db\0' + source.parent.name + '\0' + aid),
                                          'title': titles.get(book_id, '书籍 ' + book_id), 'kind': 'note' if kind == 3 else 'highlight',
                                          'text': str(body), 'location': location, 'metadata': '', 'source': '设备标注数据库', 'pending': not bool(body)})
                    finally:
                        db.close()
            except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
                warnings.append('一个标注数据库读取失败；已有摘录仍保留在本地。')
        return notes, stores

    def import_reading_sessions(self, warnings):
        try:
            source = self.device_path('system/fmcache/fmcache.db')
            if not source.is_file():
                warnings.append('设备没有可用的阅读会话缓存；已导入的阅读日历仍保留。')
                return 0
            sessions, skipped = {}, 0
            with tempfile.TemporaryDirectory(dir=self.data) as temp:
                path = self.snapshot_db(source, Path(temp))
                db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
                try:
                    for (raw,) in db.execute('SELECT session FROM reading_sessions'):
                        try:
                            session = json.loads(raw)
                            payload = session['payload']
                            if session['schema_name'] != 'reading_session' or session['schema_version'] != 0:
                                raise ValueError('未知会话版本')
                            if payload.get('is_complete') is not True:
                                continue
                            start, end = payload['start_timestamp'], payload['end_timestamp']
                            if type(start) is not int or type(end) is not int or not 1167609600000 <= start < end <= (time.time() + 300) * 1000:
                                raise ValueError('会话时间无效')
                            identity = payload.get('asin') or payload.get('asset_id')
                            if not isinstance(identity, str) or not identity:
                                raise ValueError('会话缺少书籍标识')
                            key = digest(json.dumps([identity, start], ensure_ascii=False))
                            sessions[key] = (start, end)
                        except (ValueError, TypeError, KeyError, AttributeError):
                            skipped += 1
                finally:
                    db.close()
            with self.db() as db:
                added = 0
                for key, (start, end) in sessions.items():
                    added += db.execute('INSERT OR IGNORE INTO reading_sessions VALUES (?,?,?)', (key, start, end)).rowcount
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('reading_last_sync', json.dumps(time.time())))
            if skipped:
                warnings.append(f'{skipped} 条阅读会话结构或时间无效，未计入日历。')
            return added
        except (OSError, sqlite3.Error, ValueError):
            warnings.append('阅读会话缓存读取失败或结构不兼容；已导入的阅读日历仍保留。')
            return 0

    def reading_activity(self, year=None):
        today = datetime.now().date()
        year = today.year if year is None else year
        if type(year) is not int or not 2007 <= year <= 2100:
            raise ValueError('年份须在 2007–2100 之间。')
        start = int(datetime(year, 1, 1).timestamp() * 1000)
        end = int(datetime(year + 1, 1, 1).timestamp() * 1000)
        with self.lock, self.db() as db:
            sessions = db.execute('SELECT started,ended FROM reading_sessions WHERE started<? AND ended>? ORDER BY started,ended', (end, start)).fetchall()
            bounds = db.execute('SELECT min(started),max(ended) FROM reading_sessions').fetchone()
            years = {today.year, year}
            for (timestamp,) in db.execute('SELECT started FROM reading_sessions UNION SELECT ended FROM reading_sessions'):
                years.add(datetime.fromtimestamp(timestamp / 1000).year)
            synced_at = self.read_setting('reading_last_sync')
        # Merge overlapping intervals before splitting at local midnight; never count the same minute twice.
        intervals = []
        for a, b in sessions:
            a, b = max(a, start), min(b, end)
            if intervals and a <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], b)
            else:
                intervals.append([a, b])
        days = {}
        for a, b in intervals:
            while a < b:
                local = datetime.fromtimestamp(a / 1000)
                midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time())
                stop = min(b, int(midnight.timestamp() * 1000))
                day = local.date().isoformat()
                days[day] = days.get(day, 0) + (stop - a) / 1000
                a = stop
        return {'year': year, 'years': sorted(years, reverse=True), 'today': today.isoformat(), 'days': days,
                'seconds': sum(days.values()), 'active_days': len(days), 'sessions': len(sessions), 'synced_at': synced_at,
                'first_date': datetime.fromtimestamp(bounds[0] / 1000).date().isoformat() if bounds[0] else None,
                'last_date': datetime.fromtimestamp(bounds[1] / 1000).date().isoformat() if bounds[1] else None}

    def scan(self):
        with self.lock:
            if not self.connected():
                return self.view()
            warnings, books, folders = [], [], []
            if any((self.data / 'moves').glob('*.json')):
                warnings.append('发现未完成的移动记录，请先根据本机 data/moves 中的记录检查书籍及附件位置。')
            docs = self.device_path('documents')
            for parent, dirs, files in os.walk(docs):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not d.lower().endswith('.sdr') and not (Path(parent) / d).is_symlink())
                relative_folder = str(Path(parent).relative_to(docs))
                relative_folder = '' if relative_folder == '.' else relative_folder
                folders.append(relative_folder)
                for name in sorted(files):
                    p = Path(parent) / name
                    if p.suffix.lower() not in FORMATS or 'clippings' in name.lower() or p.is_symlink():
                        continue
                    p = contained(p, self.mount)
                    stat = p.stat()
                    rel = str(p.relative_to(self.mount))
                    title = re.sub(r'_[A-Z0-9]{10,32}$', '', p.stem)
                    books.append({'id': digest(rel), 'name': name, 'title': title, 'path': rel, 'folder': relative_folder,
                                  'format': p.suffix.lstrip('.').upper(), 'size': stat.st_size, 'modified': stat.st_mtime})
            added, clipping_found = 0, False
            for candidate in [docs / 'My Clippings.txt', docs / '我的剪贴.txt']:
                if candidate.is_file():
                    clipping_found = True
                    raw = contained(candidate, self.mount).read_bytes()
                    backup = self.data / 'backups'
                    backup.mkdir(exist_ok=True)
                    (backup / ('clippings-' + hashlib.sha256(raw).hexdigest()[:16] + '.txt')).write_bytes(raw)
                    try:
                        notes, skipped = parse_clippings(raw.decode('utf-8-sig'))
                        added += self.import_notes(notes)
                        if skipped:
                            warnings.append(f'{skipped} 条摘录格式未识别，原文件已备份。')
                    except UnicodeDecodeError:
                        warnings.append('摘录文件编码未识别，原文件已备份。')
            database, stores = self.database_notes(books, warnings)
            # Both sources are kept: their position systems differ, so automatic merging risks losing annotations.
            added += self.import_notes(database)
            reading_added = self.import_reading_sessions(warnings)
            usage = shutil.disk_usage(self.mount)
            self.status = {'books': books, 'folders': folders, 'last_scan': time.time(), 'storage': {'total': usage.total, 'free': usage.free, 'used': usage.used},
                           'warnings': list(dict.fromkeys(warnings)), 'added': added, 'clipping_found': clipping_found,
                           'annotation_stores': stores, 'database_records': len(database), 'reading_added': reading_added}
            self.write_setting('snapshot', self.status)
            return self.view()

    def view(self):
        with self.lock, self.db() as db:
            notes = []
            last_batch = self.read_setting('last_batch')
            for payload, imported, batch in db.execute('SELECT payload,imported,batch FROM notes ORDER BY imported DESC,id'):
                item = json.loads(payload)
                item.update(imported=imported, new=batch == last_batch)
                notes.append(item)
            transfers = [dict(name=r[0], size=r[1], created=r[2]) for r in db.execute('SELECT name,size,created FROM transfers ORDER BY id DESC LIMIT 8')]
            return {**self.status, 'connected': self.connected(), 'mount': str(self.mount), 'notes': notes, 'transfers': transfers,
                    'converter': bool(self.converter and Path(self.converter).is_file()), 'token': self.token, 'can_eject': self.can_eject,
                    'model': 'Kindle 11 · 2022', 'firmware': '5.19.2.0.1', 'last_export': self.read_setting('last_export')}

    def send(self, source, name, folder=None):
        with self.lock:
            name = safe_name(name)
            ext = Path(name).suffix.lower()
            if ext not in UPLOAD_FORMATS:
                raise ValueError('支持 EPUB、PDF、AZW3、AZW、MOBI 和 TXT。')
            self.device_path('documents')
            if ext == '.epub':
                with tempfile.TemporaryDirectory(dir=self.data) as temp:
                    output = Path(temp) / 'converted.azw3'
                    self.convert_epub(source, output)
                    return self.copy_to_device(output, str(Path(name).with_suffix('.azw3')), folder)
            return self.copy_to_device(source, name, folder)

    def convert_epub(self, source, output):
        if not self.converter:
            raise ValueError('EPUB 转换工具未安装。请安装 calibre 后重启应用。')
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', CALIBRE_CONFIG_DIRECTORY=str(self.data / 'calibre-config'))
        proc = subprocess.run([self.converter, str(source), str(output), '--output-profile', 'kindle_pw3'],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, env=env)
        if proc.returncode or not output.is_file():
            raise ValueError('EPUB 转换失败。请检查文件是否完整、是否受加密保护。')

    def copy_to_device(self, source, name, destination=None):
        folder = self.folder_path('Kindle Desk' if destination is None else destination)
        if destination is None:
            folder.mkdir(exist_ok=True)
        if not folder.is_dir():
            raise ValueError('目标分类不存在，请刷新后重新选择。')
        target = contained(folder / safe_name(name), self.mount)
        if target.exists():
            raise ValueError('设备上已有同名文件。请更改文件名后重试；原文件未覆盖。')
        if source.stat().st_size + 1024 * 1024 > shutil.disk_usage(self.mount).free:
            raise ValueError('Kindle 剩余空间不足。')
        created = False
        try:
            with target.open('xb') as out, source.open('rb') as src:
                created = True
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            if target.stat().st_size != source.stat().st_size:
                raise OSError('传输大小校验失败')
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
        with self.db() as db:
            db.execute('INSERT INTO transfers(name,size,created) VALUES (?,?,?)', (name, target.stat().st_size, time.time()))
        self.scan()
        return {'name': name, 'message': '已复制到 Kindle。安全弹出后，等待设备完成索引。'}

    def eject(self):
        with self.lock:
            if not self.can_eject:
                raise ValueError('容器模式请先停止容器，再从宿主机文件管理器安全弹出 Kindle。')
            self.device_path('documents')
            found = subprocess.run(['findmnt', '-rn', '-o', 'SOURCE', '--mountpoint', str(self.mount)], capture_output=True, text=True, timeout=10)
            device = found.stdout.strip()
            if found.returncode or not re.fullmatch(r'/dev/[a-zA-Z0-9]+', device):
                raise ValueError('无法确认设备，请在系统文件管理器中安全弹出。')
            result = subprocess.run(['udisksctl', 'unmount', '--block-device', device], capture_output=True, timeout=15)
            if result.returncode:
                raise ValueError('设备正被占用或缺少权限，请关闭占用文件后从系统文件管理器弹出。')
            return {'message': 'Kindle 已卸载，可以拔出 USB 线。'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('%s %s' % (self.log_date_time_string(), fmt % args), flush=True)

    @property
    def desk(self):
        return self.server.desk

    def arg(self, name, default=''):
        """Query parameter; a present-but-empty value is '' (blank folder means device root)."""
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True).get(name, [default])[0]

    def json_body(self, limit):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= limit:
            raise ValueError('请求为空或过大。')
        self.connection.settimeout(10)
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError('请求格式无效。')
        return payload

    @contextmanager
    def upload(self, limit, name='upload.bin', error=None):
        """Stream the request body to a temp file that lives until this block exits."""
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= limit:
            raise ValueError(error or '文件为空或过大。书籍上限 200 MB，摘录上限 10 MB。')
        with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
            source = Path(temp) / name
            remaining = length
            self.connection.settimeout(30)
            with source.open('xb') as out:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('上传中断，请重试。')
                    out.write(chunk)
                    remaining -= len(chunk)
            yield source

    def respond(self, code, body, content_type='application/json; charset=utf-8', filename=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def image(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, max-age=86400')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def download(self, source, filename):
        with source.open('rb') as stream:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile)

    def allowed(self, write=False):
        host = self.headers.get('Host', '')
        if host not in self.server.allowed_hosts:
            self.respond(403, {'error': '只允许从本机地址访问。'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            self.respond(403, {'error': '拒绝跨站请求。'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.respond(403, {'error': '拒绝跨站请求。'})
            return False
        if write and not secrets.compare_digest(self.headers.get('X-Desk-Token', ''), self.desk.token):
            self.respond(403, {'error': '页面已过期，请刷新后重试。'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == '/api/moaan':
                return self.respond(200, self.desk.moaan.view())
            if path == '/api/moaan/file':
                with self.desk.lock:
                    book, source = self.desk.moaan.file(self.arg('id'))
                    return self.download(source, book['name'])
            if path == '/api/reading':
                year = self.arg('year')
                return self.respond(200, self.desk.reading_activity(int(year) if year else None))
            if path == '/api/library':
                return self.respond(200, self.desk.library.listing(self.arg('q'), self.arg('status'), self.arg('tag'),
                                                                  int(self.arg('page', '1') or '1'), self.arg('sort', 'recent'),
                                                                  int(self.arg('rating', '0') or '0')))
            if path == '/api/library/book':
                return self.respond(200, self.desk.library.book(self.arg('id')))
            if path == '/api/cover':
                cover = self.desk.library.cover_path(self.arg('book'))
                if cover is None:
                    return self.respond(404, {'error': '这本书没有封面。'})
                return self.image(cover.read_bytes())
            if path == '/api/library/file':
                info, source = self.desk.library.file(self.arg('id'))
                return self.download(source, info['name'])
            if path == '/api/state':
                return self.respond(200, self.desk.view())
            if path == '/api/book':
                book = next((b for b in self.desk.status['books'] if b['id'] == self.arg('id')), None)
                if not book:
                    return self.respond(404, {'error': '未找到该书籍，请重新扫描。'})
                return self.respond(200, self.desk.device_path(book['path']).read_bytes(), 'application/octet-stream', book['name'])
            if path in STATIC:
                name, kind = STATIC[path]
                return self.respond(200, (BASE / 'web' / name).read_bytes(), kind)
            self.respond(404, {'error': '页面不存在。'})
        except (OSError, ValueError, sqlite3.Error):
            self.respond(409, {'error': '设备、书库或文件不可用，请检查连接和数据目录后重试。'})

    def do_POST(self):
        if not self.allowed(write=True):
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in {'/api/moaan/scan', '/api/moaan/import', '/api/moaan/send', '/api/moaan/folders'}:
                payload = self.json_body(16384)
                if path == '/api/moaan/scan':
                    return self.respond(200, self.desk.moaan.scan(payload.get('folder', '')))
                if path == '/api/moaan/folders':
                    return self.respond(200, self.desk.moaan.create_folder(payload.get('parent', ''), payload.get('name'), payload.get('device_id')))
                if not isinstance(payload.get('id'), str):
                    raise ValueError('墨案图书标识无效。')
                if path == '/api/moaan/import':
                    return self.respond(200, self.desk.moaan.import_book(payload['id']))
                return self.respond(200, self.desk.moaan.send(payload['id'], payload.get('folder'), payload.get('device_id')))
            if path == '/api/scan':
                return self.respond(200, self.desk.scan())
            if path == '/api/eject':
                return self.respond(200, self.desk.eject())
            if path == '/api/library/cover':
                file_id, generate = self.arg('id'), self.arg('generate', '0')
                if generate == '1' and self.headers.get('Content-Length', '0') == '0':
                    return self.respond(200, self.desk.library.apply_cover(file_id))
                if generate != '0':
                    raise ValueError('请选择最多 8 MiB 的 JPEG/PNG，或使用自动生成封面。')
                with self.upload(8 * 1024 * 1024, 'upload.image', '请选择最多 8 MiB 的 JPEG/PNG，或使用自动生成封面。') as image:
                    return self.respond(200, self.desk.library.apply_cover(file_id, image))
            if path in {'/api/library/update', '/api/library/send', '/api/library/delete'}:
                payload = self.json_body(131072)
                if not isinstance(payload.get('id'), str):
                    raise ValueError('图书请求格式无效。')
                if path == '/api/library/update':
                    return self.respond(200, self.desk.library.update(payload))
                if path == '/api/library/delete':
                    return self.respond(200, self.desk.library.delete(payload['id'], payload.get('delete_files', False)))
                return self.respond(200, self.desk.library.send(payload['id'], payload.get('folder')))
            if path in {'/api/folders', '/api/move'}:
                payload = self.json_body(16384)
                if path == '/api/folders':
                    return self.respond(200, self.desk.create_folder(payload.get('parent', ''), payload.get('name')))
                return self.respond(200, self.desk.move_book(payload.get('id'), payload.get('folder')))
            if path == '/api/export':
                notes = self.desk.view()['notes']
                if not notes:
                    raise ValueError('还没有可导出的摘录。')
                data = markdown_zip(notes)
                self.desk.write_setting('last_export', time.time())
                return self.respond(200, data, 'application/zip', 'Kindle摘录.zip')
            if path not in {'/api/upload', '/api/import', '/api/library/import', '/api/moaan/upload'}:
                return self.respond(404, {'error': '操作不存在。'})
            name = safe_name(self.arg('name'))
            with self.upload(10 * 1024 * 1024 if path == '/api/import' else MAX_UPLOAD, name) as source:
                if path == '/api/moaan/upload':
                    return self.respond(200, self.desk.moaan.copy_to_device(source, name, self.arg('folder'), self.arg('device_id')))
                if path == '/api/import':
                    notes, skipped = parse_clippings(source.read_text(encoding='utf-8-sig'))
                    if not notes:
                        raise ValueError('未识别到摘录，请选择 Kindle 的 My Clippings.txt。')
                    return self.respond(200, {'added': self.desk.import_notes(notes), 'skipped': skipped})
                if path == '/api/library/import':
                    return self.respond(200, self.desk.library.import_file(source, name, self.arg('book', None)))
                # A blank folder= means the device root; an absent one means the default folder.
                self.respond(200, self.desk.send(source, name, self.arg('folder', None)))
        except (ValueError, UnicodeError) as e:
            self.respond(400, {'error': str(e)})
        except subprocess.TimeoutExpired:
            self.respond(408, {'error': '操作超时，请检查设备连接后重试；同名文件不会被覆盖。'})
        except PermissionError:
            self.respond(403, {'error': '没有写入权限。请确认 Kindle 以可写方式挂载，并从本机终端启动应用。'})
        except (OSError, sqlite3.Error):
            self.respond(409, {'error': '操作失败，请检查设备连接、剩余空间与文件权限。'})


def make_server(desk, port, host='127.0.0.1'):
    server = ThreadingHTTPServer((host, port), Handler)
    server.desk = desk
    actual_port = server.server_address[1]
    server.allowed_hosts = {f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'}
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mount', default='/run/media/rose/Kindle')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='127.0.0.1', help='监听地址；容器内使用 0.0.0.0，宿主机端口仍只发布到本机')
    parser.add_argument('--no-eject', action='store_true', help='禁用宿主机设备弹出操作（容器使用）')
    parser.add_argument('--data', type=Path, default=BASE / 'data')
    parser.add_argument('--converter', default=shutil.which('ebook-convert'))
    args = parser.parse_args()
    local_converter = BASE / 'runtime/calibre/ebook-convert'
    converter = args.converter or (str(local_converter) if local_converter.is_file() else None)
    desk = Desk(args.mount, args.data, converter, can_eject=not args.no_eject)
    try:
        desk.scan()
    except (OSError, sqlite3.Error, ValueError) as e:
        desk.status['warnings'] = ['初次读取失败，请连接设备后点击刷新。']
        print(type(e).__name__, flush=True)
    server = make_server(desk, args.port, args.host)
    print(f'Kindle Desk: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
