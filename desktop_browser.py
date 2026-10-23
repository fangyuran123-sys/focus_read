"""Qt desktop shell: local library and a separate, unprivileged web browser."""
import json
import os
import shutil
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import urlencode

from PyQt6.QtCore import QLockFile, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                             QDialog, QDialogButtonBox, QListWidget, QComboBox)
from PyQt6.QtWebEngineCore import (QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile,
                                   QWebEngineSettings, QWebEngineUrlRequestInterceptor)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from desktop_downloads import (MAX_BOOK, MAX_IMAGE, IMAGE_TYPES, download_name, import_download,
                               public_https, validate_book, validate_image, local_json, apply_downloaded_cover)

BASE = Path(__file__).resolve().parent
LIBRARY_URL = 'http://127.0.0.1:8765/'
INKPAPER_URL = 'https://inkpaper.foolstack.net/'


class NetworkBoundary(QWebEngineUrlRequestInterceptor):
    def __init__(self, local, parent):
        super().__init__(parent)
        self.local = local
        self.hosts = {}

    def interceptRequest(self, info):
        url = info.requestUrl()
        if self.local:
            allowed = url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == 8765
        elif url.scheme() in {'data', 'blob', 'about'}:
            allowed = True
        else:
            key = (url.scheme(), url.host(), url.port())
            cached = self.hosts.get(key)
            if not cached or time.monotonic() - cached[0] > 60:
                allowed = public_https(url.toString())
                if len(self.hosts) > 256:
                    self.hosts.clear()
                self.hosts[key] = (time.monotonic(), allowed)
            else:
                allowed = cached[1]
        info.block(not allowed)


class LibraryPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, main_frame):
        return url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == 8765


class WebsitePage(QWebEnginePage):
    def __init__(self, profile, parent):
        super().__init__(profile, parent)
        self.newWindowRequested.connect(lambda request: self.setUrl(request.requestedUrl()))
        self.permissionRequested.connect(lambda permission: permission.deny())
        self.fileSystemAccessRequested.connect(lambda request: request.reject())
        self.desktopMediaRequested.connect(lambda request: request.cancel())

    def acceptNavigationRequest(self, url, kind, main_frame):
        return url.toString() == 'about:blank' or (url.scheme() in {'blob', 'data'} and not main_frame) or public_https(url.toString())

    def chooseFiles(self, mode, old_files, accepted_types):
        # Remote sites have no native bridge, file uploads, local-file navigation or library cookies.
        return []


class ImportJob(QThread):
    finished_import = pyqtSignal(object, str)

    def __init__(self, path, parent, file_id=None):
        super().__init__(parent)
        self.path = path
        self.file_id = file_id

    def run(self):
        try:
            result = apply_downloaded_cover(self.path, self.file_id) if self.file_id else import_download(self.path)
            self.finished_import.emit(result, '')
        except Exception as error:
            self.finished_import.emit(None, str(error))


class DeskWindow(QMainWindow):
    def __init__(self, home):
        super().__init__()
        self.home = home
        self.download_dir = home / 'downloads'
        self.download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.setWindowTitle('Kindle Desk')
        self.setWindowIcon(QIcon(str(BASE / 'web/icon.svg')))
        self.resize(1440, 960)
        self.active_downloads = {}
        self.job = None
        self.profiles = []
        self.local_view = self.webview('library', True)
        self.remote_view = self.webview('websites', False)
        self.cover_view = self.webview('inkpaper', False)
        self.cover_view.page().profile().downloadRequested.connect(lambda request: self.start_download(request, image=True))
        self.local_view.setUrl(QUrl(LIBRARY_URL))
        self.remote_view.urlChanged.connect(lambda url: self.address.setText(url.toString()))
        self.remote_view.loadFinished.connect(self.site_loaded)
        self.remote_view.page().profile().downloadRequested.connect(self.start_download)
        # Local-library downloads remain in the library's normal browser UI; never auto-import them.
        self.local_view.page().profile().downloadRequested.connect(self.save_local_download)

        shell = QWidget(); self.setCentralWidget(shell)
        layout = QHBoxLayout(shell); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        rail = QWidget(); rail.setFixedWidth(148)
        rail.setObjectName('desktopRail')
        nav = QVBoxLayout(rail); nav.setContentsMargins(14, 24, 14, 20); nav.setSpacing(12)
        nav.addWidget(QLabel('Kindle Desk'))
        self.stack = QStackedWidget()
        for label, index in [('我的图书馆', 0), ('找书 / Z-Library', 1), ('下载管理', 2), ('封面 / 壁纸', 3)]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, i=index: self.show_view(i))
            nav.addWidget(button)
        nav.addStretch()
        hint = QLabel('网站与本机隔离\n下载后确认入库'); hint.setWordWrap(True); nav.addWidget(hint)
        layout.addWidget(rail); layout.addWidget(self.stack, 1)
        self.stack.addWidget(self.local_view)
        self.stack.addWidget(self.browser_panel())
        self.stack.addWidget(self.download_panel())
        self.stack.addWidget(self.cover_panel())
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #efede5; color: #23211b; }
            QWidget#desktopRail { background: #e6e3d9; border-right: 1px solid #d9d2c4; }
            QPushButton { background: #f8f6ef; border: 1px solid #d9d2c4; border-radius: 6px; padding: 9px; }
            QPushButton:hover { background: #f2e7d3; }
            QPushButton:disabled { color: #8b877e; }
            QLineEdit { background: white; border: 1px solid #d9d2c4; border-radius: 6px; padding: 9px; }
            QTableWidget { background: #f8f6ef; gridline-color: #d9d2c4; }
            QLabel { background: transparent; }
        ''')
        self.restore_downloads()

    def webview(self, name, local):
        profile = QWebEngineProfile(name, self)
        directory = self.home / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.setPersistentStoragePath(str(directory))
        profile.setCachePath(str(directory / 'cache'))
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)
        boundary = NetworkBoundary(local, profile)
        profile.setUrlRequestInterceptor(boundary)
        profile.boundary = boundary
        view = QWebEngineView(self)
        view.setPage(LibraryPage(profile, view) if local else WebsitePage(profile, view))
        settings = view.settings()
        for option in (QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
                       QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
                       QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
                       QWebEngineSettings.WebAttribute.JavascriptCanPaste,
                       QWebEngineSettings.WebAttribute.AllowRunningInsecureContent,
                       QWebEngineSettings.WebAttribute.NavigateOnDropEnabled):
            settings.setAttribute(option, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True)
        self.profiles.append(profile)
        return view

    def browser_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel)
        toolbar = QHBoxLayout()
        for label, action in [('后退', self.remote_view.back), ('前进', self.remote_view.forward), ('刷新', self.remote_view.reload)]:
            button = QPushButton(label); button.clicked.connect(action); toolbar.addWidget(button)
        self.address = QLineEdit(); self.address.setPlaceholderText('填写你确认的 HTTPS 书源地址，不预置镜像站')
        self.address.returnPressed.connect(self.open_site); toolbar.addWidget(self.address, 1)
        go = QPushButton('打开'); go.clicked.connect(self.open_site); toolbar.addWidget(go)
        save = QPushButton('设为书源首页'); save.clicked.connect(self.save_site); toolbar.addWidget(save)
        layout.addLayout(toolbar)
        self.site_status = QLabel('填写你信任的地址。请正常登录，仅下载你有权获取的内容；登录和验证码由你操作。')
        self.site_status.setWordWrap(True); layout.addWidget(self.site_status)
        layout.addWidget(self.remote_view, 1)
        self.saved_site = ''
        try:
            value = json.loads((self.home / 'site.json').read_text())
            if isinstance(value, str) and len(value) <= 4096:
                self.saved_site = value
                self.address.setText(value)
        except (OSError, ValueError):
            pass
        return panel

    def cover_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel)
        toolbar = QHBoxLayout()
        for label, action in [('后退', self.cover_view.back), ('前进', self.cover_view.forward), ('刷新', self.cover_view.reload),
                              ('InkPaper 首页', lambda: self.cover_view.setUrl(QUrl(INKPAPER_URL)))]:
            button = QPushButton(label); button.clicked.connect(action); toolbar.addWidget(button)
        self.cover_address = QLineEdit(); self.cover_address.setReadOnly(True)
        self.cover_address.setPlaceholderText(INKPAPER_URL)
        self.cover_view.urlChanged.connect(lambda url: self.cover_address.setText(url.toString()))
        toolbar.addWidget(self.cover_address, 1); layout.addLayout(toolbar)
        self.cover_status = QLabel('InkPaper 在线浏览。图片下载后到“下载管理”预览并选择图书；不会自动改设备壁纸。')
        self.cover_status.setWordWrap(True); layout.addWidget(self.cover_status)
        self.cover_view.loadFinished.connect(lambda ok: self.cover_status.setText(
            'InkPaper 已加载。仅下载 JPEG/PNG，随后确认用于哪本书。' if ok else '页面未能加载，请检查网络或刷新；未放宽网站隔离。'))
        layout.addWidget(self.cover_view, 1)
        return panel

    def open_site(self):
        value = self.address.text().strip()
        if not value.startswith('https://'):
            value = 'https://' + value
        if len(value) > 4096 or not public_https(value):
            QMessageBox.warning(self, '地址不可用', '只允许公共 HTTPS 网站，不访问本机、局域网、文件路径或带账号密码的地址。')
            return
        self.remote_view.setUrl(QUrl(value))
        self.site_status.setText('正在加载…请确认地址栏域名，网站登录状态独立于本地图书馆。')

    def save_site(self):
        url = self.remote_view.url()
        value = url.toString(QUrl.UrlFormattingOption.RemoveQuery | QUrl.UrlFormattingOption.RemoveFragment)
        if not public_https(value):
            QMessageBox.warning(self, '未保存', '请先打开并确认公共 HTTPS 网站。')
            return
        try:
            (self.home / 'site.json').write_text(json.dumps(value), encoding='utf-8')
        except OSError as error:
            QMessageBox.warning(self, '未保存', str(error)); return
        self.saved_site = value
        self.site_status.setText('已保存书源首页（不保存 URL 查询参数）。')

    def site_loaded(self, success):
        self.site_status.setText('网站已加载。下载完成后到“下载管理”确认入库。' if success else
                                 '网页未能加载。请检查地址、网络或网站限制；本机/局域网和非 HTTPS 请求会被阻止。')

    def show_view(self, index):
        self.stack.setCurrentIndex(index)
        if index == 1 and self.remote_view.url().isEmpty() and self.saved_site:
            self.address.setText(self.saved_site); self.open_site()
        elif index == 3 and self.cover_view.url().isEmpty():
            self.cover_view.setUrl(QUrl(INKPAPER_URL))

    def download_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel)
        title = QLabel('下载完成后，确认加入本地图书库'); layout.addWidget(title)
        self.download_hint = QLabel('电子书 ≤200 MiB；封面 JPEG/PNG ≤8 MiB、2000 万像素。完成后确认入库或选择图书补封面；失败文件不使用，下载原件保留。')
        self.download_hint.setWordWrap(True); layout.addWidget(self.download_hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['文件', '状态', '操作'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 400); self.table.setColumnWidth(1, 300)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        return panel

    def add_row(self, name, status):
        row = self.table.rowCount(); self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name)); self.set_status(row, status)
        return row

    def set_status(self, row, text):
        self.table.setItem(row, 1, QTableWidgetItem(text))

    def restore_downloads(self):
        for path in sorted(self.download_dir.glob('*/*'), key=lambda p: p.name):
            if path.is_symlink() or path.parent.is_symlink() or path.suffix.lower() not in {'.epub', '.pdf', '.azw', '.azw3', '.mobi', '.prc', '.txt'} | IMAGE_TYPES:
                continue
            row = self.add_row(path.name, '已下载，待确认')
            if path.suffix.lower() in IMAGE_TYPES:
                self.cover_button(row, path)
            else:
                self.import_button(row, path)

    def start_download(self, download, image=False):
        try:
            name = download_name(download.downloadFileName(), image=image)
            limit = MAX_IMAGE if image else MAX_BOOK
            url = download.url().toString()
            blob = image and url.startswith('blob:') and public_https(url[5:])
            if download.totalBytes() > limit or not (blob or public_https(url)):
                raise ValueError('下载超出大小限制，或来源不是公共 HTTPS 网站。')
            if max(download.totalBytes(), 1048576) + 1048576 > shutil.disk_usage(self.download_dir).free:
                raise ValueError('下载目录剩余空间不足。')
        except ValueError as error:
            download.cancel(); QMessageBox.warning(self, '未下载', str(error)); return
        folder = self.download_dir / uuid.uuid4().hex
        try:
            folder.mkdir(mode=0o700)
        except OSError as error:
            download.cancel(); QMessageBox.warning(self, '无法创建下载目录', str(error)); return
        path = folder / name
        row = self.add_row(name, '等待下载')
        download.setDownloadDirectory(str(folder)); download.setDownloadFileName('payload.part')
        cancel = QPushButton('取消'); cancel.clicked.connect(download.cancel)
        self.table.setCellWidget(row, 2, cancel)
        self.active_downloads[(image, download.id())] = download
        download.receivedBytesChanged.connect(lambda: self.download_progress(download, row, limit))
        download.isFinishedChanged.connect(lambda: self.download_finished(download, row, path, image))
        download.accept()
        self.stack.setCurrentIndex(2)

    def download_progress(self, download, row, limit=MAX_BOOK):
        received = download.receivedBytes()
        if received > limit:
            download.cancel(); self.set_status(row, '超出大小限制，已取消，不使用'); return
        self.set_status(row, '下载中 %.1f MiB' % (received / 1048576))

    def download_finished(self, download, row, path, image=False):
        if not download.isFinished():
            return
        self.active_downloads.pop((image, download.id()), None)
        self.table.removeCellWidget(row, 2)
        if download.state() != QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            self.set_status(row, '未完成：' + download.interruptReasonString()); return
        partial = path.parent / 'payload.part'
        try:
            if partial.stat().st_size > (MAX_IMAGE if image else MAX_BOOK):
                raise ValueError('下载超出大小限制，不使用。')
            partial.rename(path)
            if image:
                validate_image(path)
            else:
                validate_book(path)
        except (OSError, ValueError) as error:
            self.set_status(row, '需检查：' + str(error)); return
        self.set_status(row, '图片已完成，待确认用途' if image else '已完成，待确认入库')
        if image:
            self.cover_button(row, path)
        else:
            self.import_button(row, path)

    def cover_button(self, row, path):
        button = QPushButton('预览 / 用作封面')
        button.clicked.connect(lambda: self.choose_cover_book(row, path, button))
        self.table.setCellWidget(row, 2, button)

    def choose_cover_book(self, row, path, button):
        if self.job and self.job.isRunning():
            QMessageBox.information(self, '正在处理', '请等待当前任务完成。'); return
        try:
            validate_image(path)
        except ValueError as error:
            QMessageBox.warning(self, '图片不可用', str(error)); return
        dialog = QDialog(self); dialog.setWindowTitle('选择图书补封面'); dialog.resize(760, 560)
        layout = QVBoxLayout(dialog)
        preview = QLabel(); preview.setPixmap(QPixmap(str(path)).scaledToHeight(220)); layout.addWidget(preview)
        hint = QLabel('确认后直接更新所选书库文件，不新增版本；EPUB/AZW3/MOBI 保持格式，TXT/AZW 替换为 AZW3。下载源文件和图片保留，不自动发送或更换设备壁纸。')
        hint.setWordWrap(True); layout.addWidget(hint)
        line = QHBoxLayout(); search = QLineEdit(); search.setPlaceholderText('搜索本地图书书名或作者')
        find = QPushButton('搜索'); line.addWidget(search, 1); line.addWidget(find); layout.addLayout(line)
        books = QListWidget(); layout.addWidget(books, 1)
        files = QComboBox(); layout.addWidget(files)
        error_label = QLabel(''); error_label.setWordWrap(True); layout.addWidget(error_label)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Ok).setText('更新所选书库封面')
        controls.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addWidget(controls)
        current_books = []

        def select_book():
            files.clear(); controls.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            index = books.currentRow()
            if index < 0 or index >= len(current_books): return
            try:
                book = local_json('/api/library/book?' + urlencode({'id': current_books[index]['id']}))
                for file in book['files']:
                    if file['format'] in {'EPUB', 'AZW', 'AZW3', 'MOBI', 'TXT'}:
                        files.addItem(file['name'] + ' / ' + file['format'], file['id'])
                controls.button(QDialogButtonBox.StandardButton.Ok).setEnabled(files.count() > 0)
                error_label.setText('' if files.count() else '此书没有可补封面的格式，PDF/KFX/PRC 暂不支持。')
            except Exception as error:
                error_label.setText(str(error))

        def find_books():
            books.clear(); current_books.clear()
            try:
                result = local_json('/api/library?' + urlencode({'q': search.text().strip()}))
                current_books.extend(result['books'])
                for book in current_books: books.addItem(book['title'] + ' / ' + (book['author'] or '作者待补充'))
                error_label.setText('找到 ' + str(result['total']) + ' 本，显示前 36 本；可缩小关键词范围。')
            except Exception as error:
                error_label.setText(str(error))

        books.currentRowChanged.connect(select_book)
        search.returnPressed.connect(find_books); find.clicked.connect(find_books)
        controls.accepted.connect(dialog.accept); controls.rejected.connect(dialog.reject)
        find_books()
        if dialog.exec() != QDialog.DialogCode.Accepted or not files.currentData(): return
        if files.currentText().endswith((' / TXT', ' / AZW')) and QMessageBox.question(self, '此文件将替换为 AZW3',
                'TXT 无法内嵌封面，旧 AZW 格式需转换。将替换这条书库文件为 AZW3，不再保留书库中的旧格式。下载源文件不动，确认继续？') != QMessageBox.StandardButton.Yes:
            return
        button.setEnabled(False); self.set_status(row, '正在更新书库封面，不新增版本…')
        job = ImportJob(path, self, file_id=files.currentData()); self.job = job
        job.finished_import.connect(lambda result, error: self.cover_finished(row, button, result, error))
        job.start()

    def cover_finished(self, row, button, result, error):
        button.setEnabled(True)
        self.set_status(row, '补封面失败：' + error if error else result['message'])
        if not error: self.local_view.reload()

    def import_button(self, row, path):
        button = QPushButton('加入书库')
        button.clicked.connect(lambda: self.start_import(row, path, button))
        self.table.setCellWidget(row, 2, button)

    def start_import(self, row, path, button):
        if self.job and self.job.isRunning():
            QMessageBox.information(self, '正在导入', '请等待当前文件导入完成。'); return
        if QMessageBox.question(self, '加入本地书库', '导入“' + path.name + '”？\n按内容去重，保留下载原件，不自动发送到阅读器。') != QMessageBox.StandardButton.Yes:
            return
        button.setEnabled(False); self.set_status(row, '正在校验并导入…')
        job = ImportJob(path, self); self.job = job
        job.finished_import.connect(lambda result, error: self.import_finished(row, button, result, error))
        job.start()

    def import_finished(self, row, button, result, error):
        if error:
            self.set_status(row, '导入失败：' + error); button.setEnabled(True)
        else:
            self.set_status(row, ('书库已有，未重复导入' if result['duplicate'] else '已加入书库') + ('；' + result['warning'] if result.get('warning') else ''))
            button.setText('已入库')
            self.local_view.reload()

    def save_local_download(self, download):
        # Use Qt's native save dialog without granting remote sites a file bridge.
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, '保存图书馆文件', download.downloadFileName())
        if path:
            download.setDownloadDirectory(str(Path(path).parent)); download.setDownloadFileName(Path(path).name); download.accept()
        else:
            download.cancel()

    def dispose(self):
        # Qt profiles must outlive their pages, including during application shutdown.
        from PyQt6 import sip
        for view in (self.local_view, self.remote_view, self.cover_view):
            sip.delete(view.page())
        for profile in self.profiles:
            sip.delete(profile)

    def closeEvent(self, event):
        if self.job and self.job.isRunning():
            QMessageBox.information(self, '正在导入', '文件导入完成后再关闭，避免中断操作。'); event.ignore(); return
        if self.active_downloads:
            if QMessageBox.question(self, '关闭窗口', '有正在下载的文件，确认取消下载并关闭？') != QMessageBox.StandardButton.Yes:
                event.ignore(); return
            for item in list(self.active_downloads.values()):
                item.cancel()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Kindle Desk')
    home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'kindle-desk/qt'
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(home / 'desktop.lock'))
    if not lock.tryLock(0):
        QMessageBox.information(None, 'Kindle Desk 已打开', '请使用已打开的桌面窗口，避免重复打开同一浏览器配置。')
        return 0
    window = DeskWindow(home); window.show()
    app.aboutToQuit.connect(window.dispose)
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
