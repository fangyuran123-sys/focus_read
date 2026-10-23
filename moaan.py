"""Moaan EPD105 over the current Linux user's existing GVfs MTP mount."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import urllib.parse

from library import FORMATS, safe_name

SEND_FORMATS = {'.epub', '.pdf', '.txt'}


def directory_name(name):
    name = safe_name(name)
    if name.startswith('.') or name.endswith((' ', '.')) or any(c in name for c in '<>:"|?*'):
        raise ValueError('目录名称不能隐藏或包含特殊字符。')
    return name


class Moaan:
    def __init__(self, desk):
        self.desk = desk
        self.runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
        self.snapshot = desk.read_setting('moaan_snapshot', {'books': [], 'folders': [], 'folder': '', 'last_scan': None})

    def root(self):
        # Only the inspected EPD105 family, never an arbitrary phone's MTP storage.
        roots = [storage for mount in (self.runtime / 'gvfs').glob('mtp:host=Allwinner_EPD105_*')
                 if not mount.is_symlink() for storage in mount.iterdir()
                 if storage.is_dir() and not storage.is_symlink() and (storage / 'Books').is_dir()]
        if len(roots) != 1:
            raise ValueError('未找到唯一可用的墨案 EPD105 存储。请解锁、选择文件传输，并在文件管理器打开内部存储；多台设备请只保留一台。')
        return roots[0]

    def identity(self, root):
        return hashlib.sha256(str(root).encode()).hexdigest()

    def path(self, root, relative):
        if not isinstance(relative, str) or len(relative) > 2048 or '\\' in relative or any(ord(c) < 32 for c in relative):
            raise ValueError('墨案目录无效。')
        parts = relative.split('/') if relative else []
        if any(not part or part.startswith('.') for part in parts) or (parts and parts[0] == 'Android'):
            raise ValueError('只允许访问共享书籍目录，不访问隐藏目录或 Android 应用数据。')
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('墨案路径不能包含符号链接。')
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('墨案路径超出共享存储。')
        return path

    def view(self):
        try:
            root = self.root()
            connected = True
            same = self.snapshot.get('device_id') == self.identity(root)
        except (OSError, ValueError):
            connected = False
            same = True
        snapshot = self.snapshot if same else {'books': [], 'folders': [], 'folder': '', 'last_scan': None}
        return {**snapshot, 'connected': connected, 'model': '墨案迷你阅 · EPD105', 'transport': 'MTP',
                'transfers': self.desk.read_setting('moaan_transfers', []),
                'reading_supported': False, 'reading_message': '设备可能保存阅读时长，但当前 MTP 共享目录未提供已验证的统计格式；尚未导入，不代表没有阅读记录。'}

    def scan(self, folder=''):
        with self.desk.lock:
            root = self.root()
            directory = self.path(root, folder)
            if not directory.is_dir():
                raise ValueError('墨案目录不存在，请返回内部存储重新选择。')
            device_id = self.identity(root)
            books, folders = [], []
            # Browse one folder at a time: do not crawl private app caches or the whole MTP device.
            for path in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
                if path.name.startswith('.') or path.is_symlink() or (not folder and path.name == 'Android'):
                    continue
                relative = path.relative_to(root).as_posix()
                if path.is_dir():
                    folders.append({'name': path.name, 'path': relative})
                elif path.suffix.lower() in FORMATS:
                    stat = path.stat()
                    books.append({'id': hashlib.sha256((device_id + '\0' + relative).encode()).hexdigest(),
                                  'name': path.name, 'title': path.stem, 'path': relative,
                                  'format': path.suffix[1:].upper(), 'size': stat.st_size, 'modified': stat.st_mtime})
            storage = None
            try:
                from gi.repository import Gio, GLib
                info = Gio.File.new_for_uri(self.uri(root)).query_filesystem_info('filesystem::size,filesystem::free', None)
                total, free = info.get_attribute_uint64('filesystem::size'), info.get_attribute_uint64('filesystem::free')
                if total > 0 and 0 <= free <= total:
                    storage = {'total': total, 'free': free, 'used': total - free}
            except ImportError:
                pass
            except GLib.Error:
                pass
            self.snapshot = {'device_id': device_id, 'books': books, 'folders': folders, 'folder': folder, 'last_scan': time.time(), 'storage': storage}
            self.desk.write_setting('moaan_snapshot', self.snapshot)
            return self.view()

    def file(self, book_id):
        root = self.root()
        if self.snapshot.get('device_id') != self.identity(root):
            raise ValueError('墨案设备已变化，请刷新后重新选择。')
        book = next((b for b in self.snapshot['books'] if b['id'] == book_id), None)
        if not book:
            raise ValueError('墨案书架已变化，请刷新后重新选择图书。')
        path = self.path(root, book['path'])
        if not path.is_file() or path.stat().st_size != book['size'] or path.stat().st_mtime != book['modified']:
            raise ValueError('墨案图书已变化或缺失，请刷新后重试。')
        return book, path

    def import_book(self, book_id):
        with self.desk.lock:
            book, path = self.file(book_id)
            if not 0 < book['size'] <= 200 * 1024 * 1024:
                raise ValueError('文件为空或超过 200 MiB。')
            safe_name(book['name'])
            if 2 * book['size'] + 1024 * 1024 > shutil.disk_usage(self.desk.data).free:
                raise ValueError('本地空间不足，无法安全暂存并导入墨案图书。')
            with tempfile.TemporaryDirectory(dir=self.desk.data) as temp:
                source = Path(temp) / ('book.' + book['format'].lower())
                shutil.copyfile(path, source)
                if source.stat().st_size != book['size']:
                    raise ValueError('墨案读取不完整，请检查连接后重试。')
                return self.desk.library.import_file(source, book['name'])

    def uri(self, path):
        mount = next(parent for parent in (path, *path.parents) if parent.name.startswith('mtp:host='))
        return 'mtp://' + mount.name.removeprefix('mtp:host=') + '/' + urllib.parse.quote(path.relative_to(mount).as_posix(), safe='/')

    def target_folder(self, folder, device_id):
        root = self.root()
        if device_id != self.identity(root):
            raise ValueError('墨案设备已变化，请先打开墨案设备页并刷新。')
        directory = self.path(root, folder)
        if not directory.is_dir():
            raise ValueError('墨案目标目录不存在，请刷新后重新选择。')
        return root, directory

    def create_folder(self, parent, name, device_id):
        with self.desk.lock:
            _, directory = self.target_folder(parent, device_id)
            name = directory_name(name)
            if any(p.name.casefold() == name.casefold() for p in directory.iterdir()):
                raise ValueError('该位置已有同名文件或目录，未修改。')
            try:
                from gi.repository import Gio, GLib
            except ImportError as error:
                raise ValueError('MTP 新建目录需要系统 python3-gi（GIO）。') from error
            try:
                Gio.File.new_for_uri(self.uri(directory / name)).make_directory(None)
            except GLib.Error as error:
                raise ValueError('目录创建未成功，请检查连接及权限；已有文件不会覆盖，请刷新确认。') from error
            return self.scan(parent)

    def copy_to_device(self, source, name, folder, device_id):
        with self.desk.lock:
            _, directory = self.target_folder(folder, device_id)
            if not folder:
                raise ValueError('请先在墨案设备页选择现有的书籍目录，例如 Books。')
            name = safe_name(name)
            if Path(name).suffix.lower() not in SEND_FORMATS:
                raise ValueError('墨案发送入口支持 EPUB、PDF、TXT，EPUB 直接发送，不转换。')
            if not 0 < source.stat().st_size <= 200 * 1024 * 1024:
                raise ValueError('文件为空或超过 200 MiB。')
            if any(p.name.casefold() == name.casefold() for p in directory.iterdir()):
                raise ValueError('墨案目标目录已有同名文件，未覆盖。')
            target = directory / name
            try:
                from gi.repository import Gio, GLib
            except ImportError as error:
                raise ValueError('MTP 发送需要系统 python3-gi（GIO）；也可先下载后用文件管理器复制。') from error
            try:
                # Unlike `gio copy` CLI, FileCopyFlags.NONE rejects concurrent destination creation.
                Gio.File.new_for_path(str(source)).copy(Gio.File.new_for_uri(self.uri(target)), Gio.FileCopyFlags.NONE, None, None, None)
            except GLib.Error as error:
                raise ValueError('墨案传输未完成，请检查连接和空间；已有文件不会覆盖，若出现残留文件请先在设备上检查。') from error
            with source.open('rb') as original, target.open('rb') as copied:
                if hashlib.file_digest(original, 'sha256').digest() != hashlib.file_digest(copied, 'sha256').digest():
                    raise ValueError('墨案副本校验失败，请在设备检查目标文件后重试。')
            history = self.desk.read_setting('moaan_transfers', [])
            history.insert(0, {'name': name, 'folder': folder, 'size': source.stat().st_size, 'created': time.time()})
            self.desk.write_setting('moaan_transfers', history[:8])
            return {'message': '已发送到墨案 ' + folder + '，并通过 SHA-256 校验。请在设备阅读器中扫描或打开该目录。'}

    def send(self, file_id, folder, device_id):
        with self.desk.lock:
            info, source = self.desk.library.file(file_id)
            result = self.copy_to_device(source, info['name'], folder, device_id)
            with self.desk.db() as db:
                db.execute('UPDATE library_files SET sent_at=?,sent_path=? WHERE id=?',
                           (time.time(), '墨案 / ' + folder + '/' + info['name'], file_id))
            return result
