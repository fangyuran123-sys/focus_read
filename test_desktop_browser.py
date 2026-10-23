"""Qt smoke tests. Run offscreen with a real Chromium sandbox, not --no-sandbox."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

from pathlib import Path
import tempfile
import unittest
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

try:
    from PyQt6.QtCore import QObject, QUrl, pyqtSignal
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage
    from desktop_browser import DeskWindow, NetworkBoundary
    QT = True
except ImportError:
    QT = False


@unittest.skipUnless(QT, 'PyQt6 WebEngine not installed')
class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['kindle-desk-tests'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = DeskWindow(Path(self.temp.name))

    def tearDown(self):
        self.window.dispose()
        self.window.deleteLater()
        from PyQt6.QtTest import QTest
        QTest.qWait(200)  # Chromium flushes its profile files asynchronously after the QObject is deleted.
        self.temp.cleanup()

    def test_profiles_pages_and_file_selection_are_isolated(self):
        window = self.window
        self.assertEqual(window.stack.count(), 4)
        self.assertIsNot(window.cover_view.page().profile(), window.remote_view.page().profile())
        self.assertEqual(window.cover_view.page().chooseFiles(None, [], []), [])
        self.assertIsNot(window.local_view.page().profile(), window.remote_view.page().profile())
        self.assertEqual(window.remote_view.page().chooseFiles(None, [], []), [])
        kind = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        self.assertFalse(window.local_view.page().acceptNavigationRequest(QUrl('https://example.com'), kind, True))
        self.assertTrue(window.local_view.page().acceptNavigationRequest(QUrl('http://127.0.0.1:8765/'), kind, True))
        self.assertFalse(window.remote_view.page().acceptNavigationRequest(QUrl('file:///etc/passwd'), kind, True))
        self.assertFalse(window.remote_view.page().acceptNavigationRequest(QUrl('http://127.0.0.1:8765/'), kind, True))
        class Request:
            def __init__(self, url): self.url = QUrl(url); self.blocked = False
            def requestUrl(self): return self.url
            def block(self, value): self.blocked = value
        boundary = window.profiles[1].boundary
        with patch('desktop_browser.public_https', return_value=False):
            request = Request('https://private.test/'); boundary.interceptRequest(request)
            self.assertTrue(request.blocked)
        request = Request('http://127.0.0.1:8765/api/state')
        window.profiles[1].boundary.interceptRequest(request)
        self.assertTrue(request.blocked)
        window.profiles[0].boundary.interceptRequest(request)
        self.assertFalse(request.blocked)

    def test_completed_download_validates_then_exposes_import_never_auto_imports(self):
        class Download(QObject):
            receivedBytesChanged = pyqtSignal()
            isFinishedChanged = pyqtSignal()
            def __init__(self): super().__init__(); self.finished = False; self.cancelled = False
            def downloadFileName(self): return 'Book.txt'
            def totalBytes(self): return 20
            def receivedBytes(self): return 20
            def url(self): return QUrl('https://books.example/Book.txt')
            def id(self): return 1
            def setDownloadDirectory(self, directory): self.directory = Path(directory)
            def setDownloadFileName(self, filename): self.filename = filename
            def accept(self): pass
            def cancel(self): self.cancelled = True
            def isFinished(self): return self.finished
            def state(self): return QWebEngineDownloadRequest.DownloadState.DownloadCompleted
        download = Download()
        with patch('desktop_browser.public_https', return_value=True):
            self.window.start_download(download)
        self.assertEqual(download.filename, 'payload.part')
        self.assertEqual(len(self.window.active_downloads), 1)
        (download.directory / download.filename).write_text('downloaded text')
        download.finished = True; download.isFinishedChanged.emit()
        self.assertEqual(len(self.window.active_downloads), 0)
        self.assertTrue((download.directory / 'Book.txt').is_file())
        self.assertEqual(self.window.table.cellWidget(0, 2).text(), '加入书库')
        self.assertIsNone(self.window.job)

    def test_real_webengine_https_download_completes_into_pending_directory(self):
        from PyQt6.QtTest import QTest
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/Book.txt':
                    body = '独立测试电子书正文'.encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', 'attachment; filename="Book.txt"')
                else:
                    body = b'<html><a id="book" href="/Book.txt">Download</a></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        root = Path(self.temp.name)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                        '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        loaded = []
        page = self.window.remote_view.page()
        # Test fixture only: production never permits localhost or ignores certificate errors.
        page.certificateError.connect(lambda error: error.acceptCertificate())
        page.loadFinished.connect(lambda success: loaded.append(success))
        try:
            with patch('desktop_browser.public_https', return_value=True):
                page.setUrl(QUrl('https://127.0.0.1:' + str(server.server_address[1]) + '/'))
                limit = time.monotonic() + 15
                while not loaded and time.monotonic() < limit: QTest.qWait(50)
                self.assertEqual(loaded, [True])
                page.runJavaScript('document.querySelector("#book").click()')
                while time.monotonic() < limit:
                    QTest.qWait(50)
                    if self.window.table.rowCount() and not self.window.active_downloads: break
                self.assertEqual(self.window.table.rowCount(), 1)
                self.assertIn('待确认', self.window.table.item(0, 1).text())
                self.assertEqual(self.window.table.cellWidget(0, 2).text(), '加入书库')
                self.assertEqual(next(self.window.download_dir.glob('*/Book.txt')).read_text(), '独立测试电子书正文')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_image_blob_download_is_verified_and_not_imported_as_book(self):
        from PIL import Image
        class Download(QObject):
            receivedBytesChanged = pyqtSignal()
            isFinishedChanged = pyqtSignal()
            def __init__(self): super().__init__(); self.finished = False
            def downloadFileName(self): return 'wallpaper.jpeg'
            def totalBytes(self): return 1000
            def receivedBytes(self): return 1000
            def url(self): return QUrl('blob:https://inkpaper.foolstack.net/some-blob')
            def id(self): return 1
            def setDownloadDirectory(self, directory): self.directory = Path(directory)
            def setDownloadFileName(self, filename): self.filename = filename
            def accept(self): pass
            def cancel(self): self.cancelled = True
            def isFinished(self): return self.finished
            def state(self): return QWebEngineDownloadRequest.DownloadState.DownloadCompleted
        request = Download()
        with patch('desktop_browser.public_https', side_effect=lambda url: url.startswith('https://inkpaper.foolstack.net/')):
            self.window.start_download(request, image=True)
        Image.new('RGB', (720, 1280), 'white').save(request.directory / request.filename, format='JPEG')
        request.finished = True; request.isFinishedChanged.emit()
        self.assertEqual(self.window.table.cellWidget(0, 2).text(), '预览 / 用作封面')
        self.assertIsNone(self.window.job)
        self.assertTrue((request.directory / 'wallpaper.jpeg').exists())
        count = self.window.table.rowCount(); self.window.restore_downloads()
        self.assertEqual(self.window.table.rowCount(), count + 1)
        self.assertEqual(self.window.table.cellWidget(count, 2).text(), '预览 / 用作封面')
        self.window.cover_finished(0, self.window.table.cellWidget(0, 2), None, 'conversion failure')
        self.assertIn('conversion failure', self.window.table.item(0, 1).text())

    def test_native_cover_picker_requires_confirmation_and_uses_selected_file(self):
        from PIL import Image
        from PyQt6.QtWidgets import QDialog, QListWidget, QComboBox
        from unittest.mock import MagicMock
        path = Path(self.temp.name) / 'cover.png'; Image.new('RGB', (40, 60)).save(path)
        row = self.window.add_row(path.name, 'downloaded'); self.window.cover_button(row, path)
        button = self.window.table.cellWidget(row, 2)
        def local(path):
            if path.startswith('/api/library/book'):
                return {'files': [{'id': 'epub-file', 'name': 'Book.epub', 'format': 'EPUB'}, {'id': 'pdf', 'name': 'Book.pdf', 'format': 'PDF'}]}
            return {'total': 1, 'books': [{'id': 'book', 'title': 'My book', 'author': 'Author'}]}
        def accept(dialog):
            dialog.findChild(QListWidget).setCurrentRow(0)
            combo = dialog.findChild(QComboBox)
            self.assertEqual(combo.count(), 1)
            self.assertEqual(combo.currentData(), 'epub-file')
            return QDialog.DialogCode.Accepted
        with patch('desktop_browser.local_json', side_effect=local), patch.object(QDialog, 'exec', accept), patch('desktop_browser.ImportJob') as job:
            self.window.choose_cover_book(row, path, button)
            self.assertEqual(job.call_args.kwargs['file_id'], 'epub-file')
            job.return_value.start.assert_called_once()
        self.window.job = None
        with patch('desktop_browser.local_json', side_effect=local), patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Rejected), patch('desktop_browser.ImportJob') as job:
            self.window.choose_cover_book(row, path, button)
            job.assert_not_called()
        self.assertTrue(path.exists())

    def test_unknown_size_limit_invalid_files_and_partial_download_recovery(self):
        from types import SimpleNamespace
        download = SimpleNamespace(receivedBytes=lambda: 201*1024*1024, cancel=lambda: setattr(download, 'cancelled', True))
        row = self.window.add_row('oversize', 'downloading')
        self.window.download_progress(download, row)
        self.assertTrue(download.cancelled)
        folder = self.window.download_dir / 'fixture'; folder.mkdir()
        (folder / 'payload.part').write_text('partial')
        (folder / 'Book.txt').write_text('text')
        before = self.window.table.rowCount(); self.window.restore_downloads()
        self.assertEqual(self.window.table.rowCount(), before + 1)
        self.assertEqual(self.window.table.item(before, 0).text(), 'Book.txt')


if __name__ == '__main__':
    unittest.main()
