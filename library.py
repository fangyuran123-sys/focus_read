"""Local books and their files; independent of a connected reader."""
import hashlib
import json
import os
from pathlib import Path

from cover_tools import prepare_cover
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

FORMATS = {'.epub', '.pdf', '.azw3', '.azw', '.mobi', '.txt', '.kfx', '.prc'}
STATUSES = {'want', 'reading', 'finished', 'paused'}
COVER_HINTS = (b'cover-image', b'name="cover"', b"name='cover'", b'type="cover"', b"type='cover'")


def safe_name(name):
    if not isinstance(name, str) or not name or name in {'.', '..'} or any(c in name for c in '/\\\x00'):
        raise ValueError('文件名无效。请选择单个文件。')
    if len(name.encode('utf-8')) > 220 or any(ord(c) < 32 for c in name):
        raise ValueError('文件名太长或包含控制字符，请先重命名。')
    return name


def declares_cover(path):
    """True when an EPUB's package file announces a cover, so calibre extracts it instead of rendering page 1."""
    try:
        with zipfile.ZipFile(path) as archive:
            container = archive.read('META-INF/container.xml')
            match = re.search(rb'full-path=["\']([^"\']+\.opf)["\']', container)
            if not match:
                return False
            opf = archive.read(match.group(1).decode('utf-8'))
        return any(hint in opf for hint in COVER_HINTS)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        # Unreadable layout: let calibre try rather than silently dropping a real cover.
        return True


class Library:
    def __init__(self, desk):
        self.desk = desk
        self.metadata_tool = shutil.which('ebook-meta')
        self.root = desk.data / 'library'
        self.root.mkdir(exist_ok=True)
        self.covers = desk.data / 'covers'
        self.covers.mkdir(exist_ok=True)
        with desk.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS library_books(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'want', review TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS library_files(
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES library_books(id),
                    name TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL,
                    checksum TEXT NOT NULL UNIQUE, generated_from TEXT,
                    sent_at REAL, sent_path TEXT);
                CREATE INDEX IF NOT EXISTS library_files_book ON library_files(book_id);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(library_books)')}
            for name, definition in {'progress': 'INTEGER NOT NULL DEFAULT 0', 'current_page': 'INTEGER NOT NULL DEFAULT 0',
                                     'rating': 'INTEGER NOT NULL DEFAULT 0', 'finished_at': 'REAL'}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE library_books ADD COLUMN {name} {definition}')
            file_columns = {row[1] for row in db.execute('PRAGMA table_info(library_files)')}
            if 'covered_file' not in file_columns:
                db.execute('ALTER TABLE library_files ADD COLUMN covered_file TEXT')

    def book(self, book_id):
        with self.desk.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM library_books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise ValueError('本地图书不存在，请刷新后重试。')
            book = dict(row)
            book['tags'] = json.loads(book['tags'])
            book['files'] = [dict(r) for r in db.execute('SELECT * FROM library_files WHERE book_id=? ORDER BY generated_from IS NOT NULL,name', (book_id,))]
        book['cover'] = self.ensure_cover(book)
        target = self.covers / (book['id'] + '.jpg')
        book['cover_version'] = str(target.stat().st_mtime_ns) if book['cover'] else ''
        return book

    def ensure_cover(self, book):
        target = self.covers / (book['id'] + '.jpg')
        if target.is_file() or (self.covers / (book['id'] + '.none')).exists() or not self.metadata_tool:
            return target.is_file()
        source = next((f for f in book['files'] if not f['generated_from']), None) or next(iter(book['files']), None)
        if not source or source['format'] not in {'EPUB', 'PDF', 'AZW', 'AZW3', 'MOBI', 'PRC'}:
            (self.covers / (book['id'] + '.none')).touch()
            return False
        path = self.file(source['id'])[1]
        # calibre renders page 1 for EPUBs without a cover; a blank page is worse than our typographic fallback.
        if source['format'] == 'EPUB' and not declares_cover(path):
            (self.covers / (book['id'] + '.none')).touch()
            return False
        try:
            with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                output = Path(temp) / 'cover.jpg'
                env = dict(os.environ, QT_QPA_PLATFORM='offscreen', CALIBRE_CONFIG_DIRECTORY=str(self.desk.data / 'calibre-config'))
                subprocess.run([self.metadata_tool, str(path), '--get-cover', str(output)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, env=env)
                # No cover is normal for TXT and many PDFs; the marker stops repeated subprocess attempts.
                if not output.is_file() or not 0 < output.stat().st_size <= 8 * 1024 * 1024:
                    raise ValueError('没有可用封面')
                with output.open('rb') as stream:
                    if stream.read(2) != b'\xff\xd8':
                        raise ValueError('封面不是 JPEG')
                os.replace(output, target)
                return True
        except (OSError, subprocess.SubprocessError, ValueError, sqlite3.Error):
            (self.covers / (book['id'] + '.none')).touch()
            return False

    def cover_path(self, book_id):
        book = self.book(book_id)
        return self.covers / (book['id'] + '.jpg') if book['cover'] else None

    def listing(self, query='', status='', tag='', page=1, sort='recent', rating=0):
        orders = {'recent': 'created DESC,id', 'title': 'title COLLATE NOCASE,id',
                  'author': 'author COLLATE NOCASE,title COLLATE NOCASE,id', 'rating': 'rating DESC,title COLLATE NOCASE,id'}
        if type(rating) is not int or not 0 <= rating <= 5:
            raise ValueError('评分筛选无效。')
        if sort not in orders:
            raise ValueError('排序方式无效。')
        if status and status not in STATUSES:
            raise ValueError('阅读状态无效。')
        if not 1 <= page <= 100000 or len(query) > 200 or len(tag) > 40:
            raise ValueError('搜索条件无效。')
        like = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where = "WHERE (title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')"
        args = [like, like]
        if status:
            where += ' AND status=?'
            args.append(status)
        if tag:
            where += ' AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value=?)'
            args.append(tag)
        if rating:
            where += ' AND rating>=?'
            args.append(rating)
        with self.desk.db() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM library_books ' + where, args).fetchone()[0]
            pages = max(1, (total + 35) // 36)
            page = min(page, pages)
            books = [dict(r) for r in db.execute('SELECT id,title,author,tags,status,progress,current_page,rating FROM library_books ' + where +
                                                ' ORDER BY ' + orders[sort] + ' LIMIT 36 OFFSET ?', [*args, (page - 1) * 36])]
            by_id = {b['id']: b for b in books}
            for book in books:
                book['tags'] = json.loads(book['tags'])
                book['formats'] = []
                target = self.covers / (book['id'] + '.jpg')
                book['cover'] = target.is_file()
                book['cover_version'] = str(target.stat().st_mtime_ns) if book['cover'] else ''
            if books:
                placeholders = ','.join('?' for _ in books)
                for row in db.execute('SELECT DISTINCT book_id,format FROM library_files WHERE book_id IN (' + placeholders + ') ORDER BY format', list(by_id)):
                    by_id[row['book_id']]['formats'].append(row['format'])
            counts = dict(db.execute('SELECT status,count(*) FROM library_books GROUP BY status'))
            tags = sorted({t for (raw,) in db.execute('SELECT tags FROM library_books') for t in json.loads(raw)})
            year = time.localtime().tm_year
            start = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, -1))
            end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
            stats = dict(db.execute('''SELECT count(CASE WHEN status='finished' AND finished_at>=? AND finished_at<? THEN 1 END) AS finished_year,
                                      (SELECT count(*) FROM notes) AS notes FROM library_books''', (start, end)).fetchone())
            stats.update(year=year, finished=counts.get('finished', 0), reading=counts.get('reading', 0))
        return {'books': books, 'total': total, 'page': page,
                'pages': pages, 'counts': counts, 'tags': tags, 'stats': stats}

    def metadata(self, source, name):
        fallback = {'title': Path(name).stem, 'author': ''}
        if not self.metadata_tool or Path(name).suffix.lower() not in {'.epub', '.pdf', '.azw', '.azw3', '.mobi', '.prc'}:
            return fallback, ''
        try:
            with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                opf = Path(temp) / 'metadata.opf'
                env = dict(os.environ, QT_QPA_PLATFORM='offscreen', CALIBRE_CONFIG_DIRECTORY=str(self.desk.data / 'calibre-config'))
                subprocess.run([self.metadata_tool, str(source), '--to-opf', str(opf)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, env=env)
                if opf.stat().st_size > 1024 * 1024:
                    raise ValueError('元数据过大')
                root = ET.fromstring(opf.read_bytes())
                ns = {'dc': 'http://purl.org/dc/elements/1.1/'}
                title = (root.findtext('.//dc:title', default='', namespaces=ns)).strip()
                authors = [e.text.strip() for e in root.findall('.//dc:creator', ns) if e.text and e.text.strip().casefold() != 'unknown']
                return {'title': title[:300] if title and title.casefold() != 'unknown' else fallback['title'],
                        'author': '、'.join(authors)[:300]}, ''
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
            return fallback, '元数据读取失败，已按文件名保存，可在详情中手动编辑。'

    def import_file(self, source, name, book_id=None, generated_from=None):
        # Import is copy-only: an interrupted database write may leave a reusable file, never delete the source.
        name = safe_name(name)
        ext = Path(name).suffix.lower()
        if ext not in FORMATS:
            raise ValueError('支持 EPUB、PDF、AZW3、AZW、MOBI、TXT、KFX 和 PRC。')
        size = source.stat().st_size
        if not 0 < size <= 200 * 1024 * 1024:
            raise ValueError('文件为空或超过 200 MiB。')
        with source.open('rb') as src:
            checksum = hashlib.file_digest(src, 'sha256').hexdigest()
        with self.desk.lock:
            created = not book_id
            if book_id:
                self.book(book_id)
            with self.desk.db() as db:
                old = db.execute('SELECT id,book_id FROM library_files WHERE checksum=?', (checksum,)).fetchone()
            if old:
                if book_id and old[1] != book_id:
                    raise ValueError('相同文件已属于另一本书，未重复导入。')
                self.file(old[0])
                return {'book': self.book(old[1]), 'file_id': old[0], 'duplicate': True}
            target = self.root / (checksum + ext)
            if target.is_symlink():
                raise ValueError('本地图书文件不能是符号链接。')
            if target.exists():
                with target.open('rb') as f:
                    if hashlib.file_digest(f, 'sha256').hexdigest() != checksum:
                        raise ValueError('本地图书文件校验异常，请先备份并检查数据目录。')
            else:
                if size + 1024 * 1024 > shutil.disk_usage(self.root).free:
                    raise ValueError('本地图书库空间不足。')
                with tempfile.TemporaryDirectory(dir=self.root) as temp:
                    staged = Path(temp) / 'file'
                    with source.open('rb') as src, staged.open('xb') as out:
                        shutil.copyfileobj(src, out)
                        out.flush()
                        os.fsync(out.fileno())
                    with staged.open('rb') as copied:
                        if hashlib.file_digest(copied, 'sha256').hexdigest() != checksum:
                            raise ValueError('导入校验失败，源文件可能发生变化。请重试。')
                    os.replace(staged, target)
                    directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            metadata, warning = self.metadata(source, name) if not book_id else ({}, '')
            now = time.time()
            with self.desk.db() as db:
                if not book_id:
                    book_id = secrets.token_hex(16)
                    db.execute('INSERT INTO library_books(id,title,author,created,updated) VALUES (?,?,?,?,?)',
                               (book_id, metadata['title'], metadata['author'], now, now))
                file_id = secrets.token_hex(16)
                db.execute('INSERT INTO library_files(id,book_id,name,format,size,checksum,generated_from) VALUES (?,?,?,?,?,?,?)',
                           (file_id, book_id, name, ext[1:].upper(), size, checksum, generated_from))
            result = self.book(book_id)
            if created and self.ensure_cover(result):
                result['cover'] = True
            return {'book': result, 'file_id': file_id, 'duplicate': False, 'warning': warning}

    def update(self, payload):
        fields = {'title': 300, 'author': 300, 'description': 10000, 'review': 20000}
        values = {}
        for key, limit in fields.items():
            value = payload.get(key, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError('书籍信息格式错误或内容过长。')
            values[key] = value.strip()
        if not values['title']:
            raise ValueError('书名不能为空。')
        status, tags = payload.get('status'), payload.get('tags')
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('阅读状态无效。')
        if not isinstance(tags, list) or len(tags) > 30 or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in tags):
            raise ValueError('最多 30 个标签，每个标签不超过 40 字。')
        with self.desk.lock:
            old = self.book(payload.get('id'))
            for key, limit in {'progress': 100, 'current_page': 1000000, 'rating': 5}.items():
                value = payload.get(key, old[key])
                if type(value) is not int or not 0 <= value <= limit:
                    raise ValueError('进度须为 0–100，页码须为 0–1000000，评分须为 0–5 的整数。')
                values[key] = value
            now = time.time()
            finished_at = (old['finished_at'] if old['status'] == 'finished' else now) if status == 'finished' else None
            if status == 'finished':
                values['progress'] = 100
            with self.desk.db() as db:
                db.execute('''UPDATE library_books SET title=?,author=?,description=?,review=?,status=?,tags=?,updated=?,
                              progress=?,current_page=?,rating=?,finished_at=? WHERE id=?''',
                           (values['title'], values['author'], values['description'], values['review'], status,
                            json.dumps(list(dict.fromkeys(t.strip() for t in tags)), ensure_ascii=False), now,
                            values['progress'], values['current_page'], values['rating'], finished_at, payload['id']))
            return self.book(payload['id'])

    def delete(self, book_id, delete_files=False):
        if type(delete_files) is not bool:
            raise ValueError('删除文件选项无效。')
        with self.desk.lock:
            with self.desk.db() as db:
                row = db.execute('SELECT id FROM library_books WHERE id=?', (book_id,)).fetchone()
                if row is None:
                    raise ValueError('本地图书不存在，请刷新后重试。')
                paths = [self.covers / (row[0] + ext) for ext in ('.jpg', '.none')]
                if delete_files:
                    paths += [self.root / (checksum + '.' + fmt.lower()) for checksum, fmt in
                              db.execute('SELECT checksum,format FROM library_files WHERE book_id=?', (book_id,))]
                if any(path.is_symlink() for path in paths):
                    raise ValueError('本地图书文件不能是符号链接，请检查数据目录。')
                db.execute('DELETE FROM library_files WHERE book_id=?', (book_id,))
                db.execute('DELETE FROM library_books WHERE id=?', (book_id,))
            # Commit metadata first: a cleanup failure can leave an unused file, never a broken surviving book.
            failed = []
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed.append(path.name)
            message = '已移出书库。' + ('本地副本已删除。' if delete_files else '本地文件已保留，重新导入源文件可再次加入书库。')
            if failed:
                message = '已移出书库，但部分缓存或副本未能清理，请检查数据目录权限：' + '、'.join(failed)
            return {'message': message, 'cleanup_failed': failed}

    def file(self, file_id):
        with self.desk.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM library_files WHERE id=?', (file_id,)).fetchone()
        if row is None:
            raise ValueError('图书文件不存在。')
        info = dict(row)
        path = self.root / (info['checksum'] + '.' + info['format'].lower())
        if path.is_symlink() or not path.is_file() or path.stat().st_size != info['size']:
            raise ValueError('图书文件缺失或大小异常，请从备份恢复。')
        return info, path

    def apply_cover(self, file_id, image=None):
        with self.desk.lock:
            info, source = self.file(file_id)
            if info['format'] not in {'EPUB', 'AZW3', 'AZW', 'MOBI', 'TXT'}:
                raise ValueError('补封面支持未加密 EPUB、AZW3、AZW、MOBI、TXT；PDF/KFX/PRC 暂不支持。')
            if not self.metadata_tool or (info['format'] != 'EPUB' and not self.desk.converter):
                raise ValueError('补封面需要 calibre 的 ebook-meta；Kindle 格式和 TXT 还需要 ebook-convert。')
            if image is not None and not 0 < image.stat().st_size <= 8 * 1024 * 1024:
                raise ValueError('封面图片为空或超过 8 MiB。')
            book = self.book(info['book_id'])
            target = self.covers / (book['id'] + '.jpg')
            if target.is_symlink():
                raise ValueError('封面缓存不能是符号链接。')
            with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                temp = Path(temp)
                output_format = 'AZW3' if info['format'] in {'TXT', 'AZW'} else info['format']
                cover, output = temp / 'cover.jpg', temp / ('updated.' + output_format.lower())
                prepare_cover(cover, book['title'], book['author'], image)
                env = dict(os.environ, QT_QPA_PLATFORM='offscreen', CALIBRE_CONFIG_DIRECTORY=str(self.desk.data / 'calibre-config'))
                try:
                    if info['format'] != 'EPUB':
                        subprocess.run([self.desk.converter, str(source), str(output), '--cover', str(cover),
                                        '--title', book['title'], '--authors', book['author'] or 'Unknown', '--output-profile', 'kindle_pw3'],
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, env=env)
                    else:
                        shutil.copyfile(source, output)
                        subprocess.run([self.metadata_tool, str(output), '--cover', str(cover)], check=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=env)
                    if not output.is_file() or not 0 < output.stat().st_size <= 200 * 1024 * 1024:
                        raise ValueError('生成文件为空或过大。')
                    extracted = temp / 'embedded.jpg'
                    subprocess.run([self.metadata_tool, str(output), '--get-cover', str(extracted)], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, env=env)
                    if not extracted.is_file() or extracted.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError('更新的图书未通过内嵌封面校验。')
                    # MOBI/AZW3 may resize/recompress the embedded JPEG; compare pixels, not bytes.
                    from PIL import Image, ImageChops, ImageStat
                    with Image.open(cover) as expected, Image.open(extracted) as actual:
                        if actual.width * actual.height > 20000000:
                            raise ValueError('提取的封面像素过大。')
                        a = expected.convert('RGB').resize((128, 128))
                        b = actual.convert('RGB').resize((128, 128))
                        if max(ImageStat.Stat(ImageChops.difference(a, b)).mean) > 20:
                            raise ValueError('更新的图书未通过内嵌封面校验。')
                except subprocess.CalledProcessError as error:
                    raise ValueError('补封面失败，原文件未修改。请检查格式、文件完整性或 DRM 限制。') from error
                except OSError as error:
                    raise ValueError('更新的图书未通过封面读取校验，原文件未修改。') from error
                with output.open('rb') as stream:
                    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                name = str(Path(info['name']).with_suffix('.' + output_format.lower()))
                safe_name(name)
                destination = self.root / (checksum + '.' + output_format.lower())
                if destination.is_symlink():
                    raise ValueError('书库目标文件不能是符号链接。')
                if destination.exists():
                    with destination.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                            raise ValueError('书库目标文件校验异常，请先备份。')
                old_cover = target.read_bytes() if target.exists() else None
                cleanup = []
                with self.desk.db() as db:
                    collision = db.execute('SELECT id FROM library_files WHERE checksum=? AND id<>?', (checksum, file_id)).fetchone()
                    if collision:
                        raise ValueError('相同内容已属于另一个文件条目，未替换；请先整理已有重复版本。')
                    # Invalidate only the known EPUB->AZW3 send cache; retain historical/user-added versions.
                    cache_name = str(Path(info['name']).with_suffix('.azw3'))
                    caches = [dict(zip(('id', 'checksum', 'format'), row)) for row in db.execute(
                        'SELECT id,checksum,format FROM library_files WHERE book_id=? AND generated_from=? AND name=? AND format=? AND covered_file IS NULL',
                        (book['id'], file_id, cache_name, 'AZW3'))] if info['format'] == 'EPUB' else []
                    cleanup = [self.root / (f['checksum'] + '.' + f['format'].lower()) for f in caches]
                    if any(path.is_symlink() for path in cleanup):
                        raise ValueError('转换缓存不能是符号链接。')
                # Publish the new content-addressed file before committing its DB reference.
                # A crash can leave an unused file, never a record pointing to a removed original.
                published = not destination.exists()
                if published:
                    with output.open('rb+') as stream:
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(output, destination)
                    directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                try:
                    with self.desk.db() as db:
                        for cache in caches:
                            db.execute('UPDATE library_files SET covered_file=NULL WHERE covered_file=?', (cache['id'],))
                            db.execute('UPDATE library_files SET generated_from=NULL WHERE generated_from=?', (cache['id'],))
                            db.execute('DELETE FROM library_files WHERE id=?', (cache['id'],))
                        db.execute('UPDATE library_files SET generated_from=NULL WHERE generated_from=?', (file_id,))
                        db.execute('UPDATE library_files SET covered_file=NULL WHERE covered_file=?', (file_id,))
                        db.execute('''UPDATE library_files SET checksum=?,format=?,name=?,size=?,generated_from=NULL,
                                      covered_file=NULL,sent_at=NULL,sent_path=NULL WHERE id=?''',
                                   (checksum, output_format, name, destination.stat().st_size, file_id))
                        db.execute('UPDATE library_books SET updated=? WHERE id=?', (time.time(), book['id']))
                        os.replace(cover, target)
                except (OSError, sqlite3.Error):
                    if old_cover is not None:
                        restore = temp / 'restore.jpg'; restore.write_bytes(old_cover); os.replace(restore, target)
                    else:
                        target.unlink(missing_ok=True)
                    # The new file is unreferenced after rollback; keep all originals if cleanup fails.
                    if published:
                        destination.unlink(missing_ok=True)
                    raise
                if destination != source:
                    cleanup.append(source)
                failed = []
                for path in cleanup:
                    if path == destination:
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        failed.append(path.name)
            message = '封面已更新，书库文件已替换，没有新增版本。'
            if info['format'] in {'TXT', 'AZW'}:
                message += info['format'] + ' 已转换为 AZW3，原格式的书库副本不再保留。'
            message += '下载源文件和设备上的旧书未改动。'
            if failed:
                message += '部分旧文件未能清理，请检查权限：' + '、'.join(failed)
            return {'book': self.book(book['id']), 'file_id': file_id, 'message': message, 'cleanup_failed': failed}

    def send(self, file_id, folder=None):
        with self.desk.lock:
            self.desk.device_path('documents')
            info, source = self.file(file_id)
            visited = set()
            while info['covered_file']:
                if info['id'] in visited:
                    raise ValueError('封面副本关联异常，请重新补封面后重试。')
                visited.add(info['id'])
                info, source = self.file(info['covered_file'])
            if info['format'] == 'EPUB':
                book = self.book(info['book_id'])
                cached = next((f for f in book['files'] if f['generated_from'] == file_id), None)
                if not cached:
                    with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                        output = Path(temp) / 'converted.azw3'
                        self.desk.convert_epub(source, output)
                        result = self.import_file(output, str(Path(info['name']).with_suffix('.azw3')), info['book_id'], generated_from=file_id)
                        cached = next(f for f in result['book']['files'] if f['id'] == result['file_id'])
                info, source = self.file(cached['id'])
            result = self.desk.send(source, info['name'], folder)
            sent_path = str(Path('documents') / ('Kindle Desk' if folder is None else folder) / info['name'])
            with self.desk.db() as db:
                db.execute('UPDATE library_files SET sent_at=?,sent_path=? WHERE id=?',
                           (time.time(), sent_path, info['id']))
            return result
