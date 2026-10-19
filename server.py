#!/usr/bin/env python3
"""A loopback-only Kindle desk. Python standard library; calibre converts EPUB."""
import argparse
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
FORMATS = {'.azw', '.azw3', '.mobi', '.pdf', '.txt', '.kfx', '.prc'}
UPLOAD_FORMATS = {'.azw', '.azw3', '.mobi', '.pdf', '.txt', '.epub'}
MAX_UPLOAD = 200 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def safe_name(name):
    if not name or name in {'.', '..'} or any(c in name for c in '/\\\x00'):
        raise ValueError('文件名无效。请选择单个文件。')
    if len(name.encode('utf-8')) > 220 or any(ord(c) < 32 for c in name):
        raise ValueError('文件名太长或包含控制字符，请先重命名。')
    return name


def contained(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('文件路径超出了允许的目录。')
    return resolved


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
                CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL);
            ''')
        self.status = self.read_setting('snapshot', {'books': [], 'last_scan': None, 'warnings': []})

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

    def scan(self):
        with self.lock:
            if not self.connected():
                return self.view()
            warnings, books = [], []
            docs = self.device_path('documents')
            for parent, dirs, files in os.walk(docs):
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.sdr') and not (Path(parent) / d).is_symlink()]
                for name in sorted(files):
                    p = Path(parent) / name
                    if p.suffix.lower() not in FORMATS or 'clippings' in name.lower() or p.is_symlink():
                        continue
                    p = contained(p, self.mount)
                    stat = p.stat()
                    rel = str(p.relative_to(self.mount))
                    title = re.sub(r'_[A-Z0-9]{10,32}$', '', p.stem)
                    books.append({'id': digest(rel), 'name': name, 'title': title, 'path': rel,
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
            usage = shutil.disk_usage(self.mount)
            self.status = {'books': books, 'last_scan': time.time(), 'storage': {'total': usage.total, 'free': usage.free, 'used': usage.used},
                           'warnings': list(dict.fromkeys(warnings)), 'added': added, 'clipping_found': clipping_found,
                           'annotation_stores': stores, 'database_records': len(database)}
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

    def send(self, source, name):
        with self.lock:
            name = safe_name(name)
            ext = Path(name).suffix.lower()
            if ext not in UPLOAD_FORMATS:
                raise ValueError('支持 EPUB、PDF、AZW3、AZW、MOBI 和 TXT。')
            self.device_path('documents')
            if ext == '.epub':
                if not self.converter:
                    raise ValueError('EPUB 转换工具未安装。请安装 calibre 后重启应用。')
                with tempfile.TemporaryDirectory(dir=self.data) as temp:
                    output = Path(temp) / 'converted.azw3'
                    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', CALIBRE_CONFIG_DIRECTORY=str(self.data / 'calibre-config'))
                    proc = subprocess.run([self.converter, str(source), str(output), '--output-profile', 'kindle_pw3'],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, env=env)
                    if proc.returncode or not output.is_file():
                        raise ValueError('EPUB 转换失败。请检查文件是否完整、是否受加密保护。')
                    return self.copy_to_device(output, str(Path(name).with_suffix('.azw3')))
            return self.copy_to_device(source, name)

    def copy_to_device(self, source, name):
        folder = self.device_path('documents/Kindle Desk')
        folder.mkdir(exist_ok=True)
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
        path = urllib.parse.urlsplit(self.path)
        try:
            if path.path == '/api/state':
                return self.respond(200, self.desk.view())
            if path.path == '/api/book':
                book_id = urllib.parse.parse_qs(path.query).get('id', [''])[0]
                book = next((b for b in self.desk.status['books'] if b['id'] == book_id), None)
                if not book:
                    return self.respond(404, {'error': '未找到该书籍，请重新扫描。'})
                source = self.desk.device_path(book['path'])
                return self.respond(200, source.read_bytes(), 'application/octet-stream', book['name'])
            static = {'/': ('index.html', 'text/html; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
            if path.path in static:
                name, kind = static[path.path]
                return self.respond(200, (BASE / 'web' / name).read_bytes(), kind)
            self.respond(404, {'error': '页面不存在。'})
        except (OSError, ValueError):
            self.respond(409, {'error': '设备或文件不可用，请检查连接后刷新。'})

    def do_POST(self):
        if not self.allowed(write=True):
            return
        path = urllib.parse.urlsplit(self.path)
        try:
            if path.path == '/api/scan':
                return self.respond(200, self.desk.scan())
            if path.path == '/api/eject':
                return self.respond(200, self.desk.eject())
            if path.path == '/api/export':
                notes = self.desk.view()['notes']
                if not notes:
                    raise ValueError('还没有可导出的摘录。')
                data = markdown_zip(notes)
                self.desk.write_setting('last_export', time.time())
                return self.respond(200, data, 'application/zip', 'Kindle摘录.zip')
            if path.path not in {'/api/upload', '/api/import'}:
                return self.respond(404, {'error': '操作不存在。'})
            length = int(self.headers.get('Content-Length', '0'))
            limit = 10 * 1024 * 1024 if path.path == '/api/import' else MAX_UPLOAD
            if not 0 < length <= limit:
                raise ValueError('文件为空或过大。书籍上限 200 MB，摘录上限 10 MB。')
            name = safe_name(urllib.parse.parse_qs(path.query).get('name', [''])[0])
            with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                source = Path(temp) / name
                remaining = length
                self.connection.settimeout(30)
                with source.open('xb') as out:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError('文件上传中断，请重试。')
                        out.write(chunk)
                        remaining -= len(chunk)
                if path.path == '/api/import':
                    notes, skipped = parse_clippings(source.read_text(encoding='utf-8-sig'))
                    if not notes:
                        raise ValueError('未识别到摘录，请选择 Kindle 的 My Clippings.txt。')
                    added = self.desk.import_notes(notes)
                    return self.respond(200, {'added': added, 'skipped': skipped})
                self.respond(200, self.desk.send(source, name))
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
